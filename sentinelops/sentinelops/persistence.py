from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from threading import RLock
from typing import Any

from .core import (
    Event,
    EventStore,
    Incident,
    IncidentStatus,
    Signal,
    ToolResult,
    utcnow,
)


def incident_from_dict(data: dict[str, Any]) -> Incident:
    return Incident(
        title=data["title"],
        service=data["service"],
        severity=data.get("severity", "SEV-2"),
        signals=[Signal(**signal) for signal in data.get("signals", [])],
        id=data["id"],
        status=IncidentStatus(data.get("status", IncidentStatus.DETECTED.value)),
        created_at=data.get("created_at", utcnow()),
        updated_at=data.get("updated_at", utcnow()),
        summary=data.get("summary", ""),
        root_cause=data.get("root_cause", ""),
        confidence=float(data.get("confidence", 0.0)),
        runbook_ids=list(data.get("runbook_ids", [])),
        pending_actions=list(data.get("pending_actions", [])),
        executed_actions=list(data.get("executed_actions", [])),
        verification=dict(data.get("verification", {})),
    )


class MemoryIncidentStore:
    backend_name = "memory"

    def __init__(self) -> None:
        self.items: dict[str, Incident] = {}
        self._lock = RLock()

    def save(self, incident: Incident) -> None:
        with self._lock:
            self.items[incident.id] = incident

    def get(self, incident_id: str) -> Incident | None:
        return self.items.get(incident_id)

    def list(self) -> list[Incident]:
        return sorted(
            self.items.values(), key=lambda item: item.created_at, reverse=True
        )


class MemoryExecutionLedger:
    backend_name = "memory"

    def __init__(self) -> None:
        self._items: dict[str, ToolResult | None] = {}
        self._lock = RLock()

    def acquire(self, key: str) -> tuple[bool, ToolResult | None]:
        with self._lock:
            if key in self._items:
                return False, self._items[key]
            self._items[key] = None
            return True, None

    def complete(self, key: str, result: ToolResult) -> None:
        with self._lock:
            self._items[key] = result


class FirestoreIncidentStore:
    backend_name = "firestore"

    def __init__(self, client: Any, collection: str = "sentinelops_incidents") -> None:
        self.client = client
        self.collection = client.collection(collection)

    def save(self, incident: Incident) -> None:
        self.collection.document(incident.id).set(incident.to_dict())

    def get(self, incident_id: str) -> Incident | None:
        snapshot = self.collection.document(incident_id).get()
        return incident_from_dict(snapshot.to_dict()) if snapshot.exists else None

    def list(self) -> list[Incident]:
        snapshots = self.collection.order_by(
            "created_at", direction="DESCENDING"
        ).stream()
        return [incident_from_dict(snapshot.to_dict()) for snapshot in snapshots]


class FirestoreEventStore(EventStore):
    """Transactional Firestore hash chain with one stream head per incident."""

    def __init__(
        self, client: Any, collection: str = "sentinelops_event_streams"
    ) -> None:
        self.client = client
        self.collection = client.collection(collection)

    def append(
        self, incident_id: str, event_type: str, payload: dict[str, Any]
    ) -> Event:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - selected backend dependency
            raise RuntimeError(
                "install sentinelops-agent[gcp] for Firestore state"
            ) from exc

        stream_ref = self.collection.document(incident_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def append_in_transaction(txn: Any) -> dict[str, Any]:
            snapshot = stream_ref.get(transaction=txn)
            state = snapshot.to_dict() if snapshot.exists else {}
            previous_hash = state.get("head_hash", "GENESIS")
            sequence = int(state.get("next_sequence", 1))
            canonical = self._canonical(
                incident_id, event_type, payload, sequence, previous_hash
            )
            event = Event(
                incident_id=incident_id,
                event_type=event_type,
                payload=payload,
                sequence=sequence,
                previous_hash=previous_hash,
                hash=hashlib.sha256(canonical.encode()).hexdigest(),
            )
            event_ref = stream_ref.collection("events").document(f"{sequence:012d}")
            txn.set(event_ref, event.to_dict())
            txn.set(
                stream_ref,
                {
                    "incident_id": incident_id,
                    "head_hash": event.hash,
                    "next_sequence": sequence + 1,
                },
                merge=True,
            )
            return event.to_dict()

        return Event(**append_in_transaction(transaction))

    def stream(self, incident_id: str) -> list[dict[str, Any]]:
        snapshots = (
            self.collection.document(incident_id)
            .collection("events")
            .order_by("sequence")
            .stream()
        )
        return [snapshot.to_dict() for snapshot in snapshots]

    def verify(self, incident_id: str) -> bool:
        previous = "GENESIS"
        for raw in self.stream(incident_id):
            canonical = self._canonical(
                raw["incident_id"],
                raw["event_type"],
                raw["payload"],
                int(raw["sequence"]),
                previous,
            )
            expected = hashlib.sha256(canonical.encode()).hexdigest()
            if raw.get("previous_hash") != previous or raw.get("hash") != expected:
                return False
            previous = raw["hash"]
        return True

    @staticmethod
    def _canonical(
        incident_id: str,
        event_type: str,
        payload: dict[str, Any],
        sequence: int,
        previous_hash: str,
    ) -> str:
        return json.dumps(
            {
                "incident_id": incident_id,
                "event_type": event_type,
                "payload": payload,
                "sequence": sequence,
                "previous_hash": previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )


class FirestoreExecutionLedger:
    """Atomic action claim plus durable result for retry deduplication."""

    backend_name = "firestore"

    def __init__(
        self, client: Any, collection: str = "sentinelops_execution_ledger"
    ) -> None:
        self.collection = client.collection(collection)

    def acquire(self, key: str) -> tuple[bool, ToolResult | None]:
        reference = self.collection.document(key)
        try:
            reference.create({"status": "executing", "created_at": utcnow()})
            return True, None
        except Exception as exc:
            try:
                from google.api_core.exceptions import AlreadyExists
            except ImportError:  # pragma: no cover
                raise exc
            if not isinstance(exc, AlreadyExists):
                raise
        snapshot = reference.get()
        data = snapshot.to_dict() if snapshot.exists else {}
        raw = data.get("result")
        if not raw:
            return False, ToolResult(
                False, {}, "action with this idempotency key is already executing"
            )
        return False, ToolResult(
            bool(raw.get("ok")),
            dict(raw.get("output", {})),
            raw.get("message", ""),
            float(raw.get("latency_ms", 0.0)),
        )

    def complete(self, key: str, result: ToolResult) -> None:
        self.collection.document(key).set(
            {"status": "completed", "completed_at": utcnow(), "result": asdict(result)},
            merge=True,
        )


def state_from_env() -> tuple[Any, EventStore, Any]:
    backend = os.getenv("SENTINELOPS_STATE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return MemoryIncidentStore(), EventStore(), MemoryExecutionLedger()
    if backend == "firestore":
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - selected backend dependency
            raise RuntimeError(
                "install sentinelops-agent[gcp] for Firestore state"
            ) from exc
        project = os.getenv(
            "SENTINELOPS_GCP_PROJECT", os.getenv("GOOGLE_CLOUD_PROJECT")
        )
        database = os.getenv("SENTINELOPS_FIRESTORE_DATABASE", "(default)")
        client = firestore.Client(project=project, database=database)
        prefix = os.getenv("SENTINELOPS_FIRESTORE_COLLECTION_PREFIX", "sentinelops")
        return (
            FirestoreIncidentStore(client, f"{prefix}_incidents"),
            FirestoreEventStore(client, f"{prefix}_event_streams"),
            FirestoreExecutionLedger(client, f"{prefix}_execution_ledger"),
        )
    raise ValueError(f"unsupported SENTINELOPS_STATE_BACKEND: {backend}")
