"""`SIFT_APPLY_TARGET=openpipeline`: add the rule's processors to an OpenPipeline pipeline.

OpenPipeline configuration is stored as Settings 2.0 objects; the old
/platform/openpipeline/v1/configurations API is deprecated. One object per pipeline, schema
builtin:openpipeline.{logs,spans,metrics}.pipelines, scope `environment`:
https://docs.dynatrace.com/docs/dynatrace-api/environment-api/settings/schemas/builtin-openpipeline-logs-pipelines

Settings API (https://docs.dynatrace.com/docs/dynatrace-api/environment-api/settings/objects):
- GET  /api/v2/settings/objects?schemaIds=<id>&fields=objectId,value,updateToken,schemaVersion
  (`pageSize` ≤ 500, then only `nextPageKey`)  — platform token scope settings:objects:read
- GET  /api/v2/settings/objects/{objectId}  — returns `updateToken`
- PUT  /api/v2/settings/objects/{objectId}?validateOnly=  body {value, schemaVersion, updateToken}
  — scope settings:objects:write (the page adds that settings.read is needed as well).
  `updateToken` makes the write fail if the object changed since it was read.
The pages show the SaaS host https://{env}.live.dynatrace.com (DT_ENV_URL). Which host a
platform token should use for this API (.live vs .apps) is not stated: TODO(verify) on a tenant.

Sift never creates or routes pipelines. It edits the pipeline whose `customId` is
SIFT_OPENPIPELINE_PIPELINE (the pipeline the noisy-shop data is routed to), sends the new
value with validateOnly=true first, then writes it with the updateToken it read."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Protocol

import httpx

from server.agent import rules
from server.apply import (
    Applied,
    ApplyError,
    Reverted,
    check_approved,
    digest,
    fixture_state_dir,
)
from server.config import Config
from server.connectors.base import ConnectorError, request
from server.connectors.store import FixtureStore
from server.models import Proposal

OBJECTS = "/api/v2/settings/objects"
FIELDS = "objectId,value,updateToken,schemaVersion,schemaId"


class SettingsClient(Protocol):
    def list_objects(self, schema_id: str) -> list[dict[str, Any]]: ...

    def get_object(self, object_id: str) -> dict[str, Any]: ...

    def put_object(
        self,
        object_id: str,
        value: dict[str, Any],
        *,
        update_token: str | None,
        schema_version: str | None,
        validate_only: bool = False,
    ) -> dict[str, Any]: ...


class LiveSettingsClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None) -> None:
        if not base_url or not token:
            raise ApplyError("the openpipeline target needs DT_ENV_URL and DT_PLATFORM_TOKEN")
        self.http = client or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30.0,
        )

    def list_objects(self, schema_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {"schemaIds": schema_id, "fields": FIELDS, "pageSize": 500}
        while True:
            body = request(self.http, "GET", OBJECTS, params=params).json()
            items += body.get("items") or []
            key = body.get("nextPageKey")
            if not key:
                return items
            params = {"nextPageKey": key}  # the docs: omit every other parameter

    def get_object(self, object_id: str) -> dict[str, Any]:
        return request(self.http, "GET", f"{OBJECTS}/{object_id}").json()

    def put_object(
        self,
        object_id: str,
        value: dict[str, Any],
        *,
        update_token: str | None,
        schema_version: str | None,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"value": value}
        if update_token:
            body["updateToken"] = update_token
        if schema_version:
            body["schemaVersion"] = schema_version
        resp = request(
            self.http,
            "PUT",
            f"{OBJECTS}/{object_id}",
            params={"validateOnly": str(validate_only).lower()},
            json=body,
        )
        return resp.json() if resp.content else {}


class FixtureSettingsClient:
    """Fixture mode: pipelines from fixtures/dynatrace/settings/openpipeline_pipelines.json
    (GET objects response shape), writes kept in a JSON file next to the database. Behaves
    like the API on what Sift relies on: updateToken checks and validateOnly."""

    def __init__(self, store: FixtureStore, state_dir: Path) -> None:
        self.store, self.state = store, state_dir / "openpipeline.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.state.exists():
            return json.loads(self.state.read_text())
        rec = self.store.read("settings", "openpipeline_pipelines.json")["response"]
        return {
            it["objectId"]: {**it, "updateToken": "fixture-" + digest(it["value"])}
            for it in rec["items"]
        }

    def _save(self, objs: dict[str, dict[str, Any]]) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(objs, indent=1, ensure_ascii=False) + "\n")

    def list_objects(self, schema_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(o) for o in self._load().values() if o["schemaId"] == schema_id]

    def get_object(self, object_id: str) -> dict[str, Any]:
        objs = self._load()
        if object_id not in objs:
            raise ConnectorError(f"GET {OBJECTS}/{object_id} -> HTTP 404")
        return copy.deepcopy(objs[object_id])

    def put_object(
        self,
        object_id: str,
        value: dict[str, Any],
        *,
        update_token: str | None,
        schema_version: str | None,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        objs = self._load()
        if object_id not in objs:
            raise ConnectorError(f"PUT {OBJECTS}/{object_id} -> HTTP 404")
        if update_token and update_token != objs[object_id]["updateToken"]:
            raise ConnectorError(f"PUT {OBJECTS}/{object_id} -> HTTP 409: object changed")
        for k in ("customId", "displayName", "metadataList"):
            if k not in value:
                raise ConnectorError(f"PUT {OBJECTS}/{object_id} -> HTTP 400: {k} is required")
        if not validate_only:
            objs[object_id] = {
                **objs[object_id],
                "value": copy.deepcopy(value),
                "updateToken": "fixture-" + digest(value),
            }
            self._save(objs)
        return {"objectId": object_id}


# --- the pipeline value ------------------------------------------------------------------


def _own(proc_id: str, pid: str) -> bool:
    base = f"sift-{pid}"
    return proc_id == base or proc_id.startswith(base + "-")


def strip_own(value: dict[str, Any], pid: str) -> dict[str, Any]:
    out = copy.deepcopy(value)
    for stage in rules.STAGES:
        procs = (out.get(stage) or {}).get("processors")
        if procs is not None:
            out[stage]["processors"] = [p for p in procs if not _own(str(p.get("id")), pid)]
    return out


def add_rule(value: dict[str, Any], settings: dict[str, Any], pid: str) -> dict[str, Any]:
    out = strip_own(value, pid)
    for stage, proc in rules.stamped(settings, pid):
        if stage not in rules.STAGES:
            raise ApplyError(f"Sift does not write to the {stage} stage")
        procs = out.setdefault(stage, {}).setdefault("processors", [])
        # Bucket assignment is first-match-only: "no storage" must come before the rest.
        procs.insert(0, proc) if stage == "storage" else procs.append(proc)
    return out


class OpenPipelineApplier:
    target = "openpipeline"

    def __init__(self, client: SettingsClient, pipeline: str) -> None:
        if not pipeline:
            raise ApplyError(
                "set SIFT_OPENPIPELINE_PIPELINE to the customId of the pipeline the data is "
                "routed to"
            )
        self.client, self.pipeline = client, pipeline

    @classmethod
    def from_config(cls, cfg: Config) -> OpenPipelineApplier:
        if cfg.is_fixture:
            store = FixtureStore(cfg.fixtures_dir / "dynatrace")
            client: SettingsClient = FixtureSettingsClient(store, fixture_state_dir(cfg))
        else:
            client = LiveSettingsClient(cfg.dt_env_url, cfg.dt_platform_token)
        return cls(client, cfg.openpipeline_pipeline)

    def _settings(self, p: Proposal) -> dict[str, Any]:
        st = (p.rule.get("openpipeline") or {}).get("settings")
        if not st:
            raise ApplyError(f"{p.id} has no OpenPipeline processors")
        return st

    def _find(self, schema_id: str) -> dict[str, Any]:
        for item in self.client.list_objects(schema_id):
            if (item.get("value") or {}).get("customId") == self.pipeline:
                return item
        raise ApplyError(
            f"no pipeline with customId {self.pipeline!r} in {schema_id} "
            "(SIFT_OPENPIPELINE_PIPELINE)"
        )

    def snapshot(self, p: Proposal) -> dict[str, Any]:
        schema = self._settings(p)["schemaId"]
        item = self._find(schema)
        return {
            "target": self.target,
            "schemaId": schema,
            "objectId": item["objectId"],
            "schemaVersion": item.get("schemaVersion"),
            "pipeline": self.pipeline,
            "value": strip_own(item["value"], p.id),
        }

    def write(self, p: Proposal, snapshot: dict[str, Any]) -> Applied:
        check_approved(p)
        settings = self._settings(p)
        item = self._find(settings["schemaId"])
        new = add_rule(item["value"], settings, p.id)
        ids = rules.processor_ids(settings, p.id)
        ref = {
            "schemaId": settings["schemaId"],
            "objectId": item["objectId"],
            "pipeline": self.pipeline,
            "processor_ids": ids,
            "digest": digest(new),
        }
        if new == item["value"]:
            return Applied(
                self.target,
                f"Already in pipeline {self.pipeline} ({', '.join(ids)}); nothing changed",
                ref,
                changed=False,
            )
        kw = {
            "update_token": item.get("updateToken"),
            "schema_version": item.get("schemaVersion"),
        }
        try:
            self.client.put_object(item["objectId"], new, validate_only=True, **kw)
            self.client.put_object(item["objectId"], new, **kw)
        except ConnectorError as e:
            raise ApplyError(f"OpenPipeline settings write failed: {e}") from e
        return Applied(
            self.target,
            f"Added {', '.join(ids)} to OpenPipeline pipeline {self.pipeline} "
            f"({settings['schemaId']})",
            ref,
            log=["validateOnly=true: accepted", "written with updateToken"],
        )

    def revert(self, p: Proposal) -> Reverted:
        snap, ref = p.previous_state or {}, p.applied_ref or {}
        oid = snap.get("objectId") or ref.get("objectId")
        if not oid:
            return Reverted("No OpenPipeline object was written; nothing to restore", "nothing")
        try:
            obj = self.client.get_object(oid)
            current = obj["value"]
            kw = {
                "update_token": obj.get("updateToken"),
                "schema_version": obj.get("schemaVersion"),
            }
            if ref.get("digest") and digest(current) == ref["digest"] and "value" in snap:
                self.client.put_object(oid, snap["value"], **kw)
                return Reverted(
                    f"Restored pipeline {snap.get('pipeline')} from the snapshot", "snapshot"
                )
            clean = strip_own(current, p.id)
            if clean == current:
                return Reverted(f"sift-{p.id} is no longer in the pipeline", "nothing")
            self.client.put_object(oid, clean, **kw)
        except ConnectorError as e:
            raise ApplyError(f"OpenPipeline revert failed: {e}") from e
        return Reverted(
            "The pipeline changed after the rule was applied, so only this proposal's "
            "processors were removed",
            "removed_own",
        )
