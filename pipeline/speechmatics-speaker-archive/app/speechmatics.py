from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx


class SpeechmaticsError(RuntimeError):
    pass


class SpeechmaticsClient:
    def __init__(self, api_key: str, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=30.0, read=180.0, write=3600.0, pool=30.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def submit_job(self, audio_path: Path, config: dict[str, Any]) -> str:
        mime = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
        try:
            with audio_path.open("rb") as audio_file:
                response = await self.client.post(
                    f"{self.base_url}/jobs",
                    files={
                        "data_file": (audio_path.name, audio_file, mime),
                        "config": (None, json.dumps(config, ensure_ascii=False), "application/json"),
                    },
                )
        except (OSError, httpx.HTTPError) as exc:
            raise SpeechmaticsError(f"Could not submit job: {exc}") from exc
        data = self._json_or_raise(response)
        job = data.get("job", data)
        job_id = job.get("id")
        if not job_id:
            raise SpeechmaticsError(f"Speechmatics did not return a job id: {data}")
        return str(job_id)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{self.base_url}/jobs/{job_id}")
        except httpx.HTTPError as exc:
            raise SpeechmaticsError(f"Could not read job status: {exc}") from exc
        data = self._json_or_raise(response)
        return data.get("job", data)

    async def get_transcript(self, job_id: str) -> dict[str, Any]:
        try:
            response = await self.client.get(
                f"{self.base_url}/jobs/{job_id}/transcript",
                params={"format": "json-v2"},
            )
        except httpx.HTTPError as exc:
            raise SpeechmaticsError(f"Could not download transcript: {exc}") from exc
        return self._json_or_raise(response)

    @staticmethod
    def _json_or_raise(response: httpx.Response) -> dict[str, Any]:
        if response.is_error:
            text = response.text[:2000]
            raise SpeechmaticsError(f"Speechmatics HTTP {response.status_code}: {text}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SpeechmaticsError("Speechmatics returned non-JSON data") from exc
        if not isinstance(data, dict):
            raise SpeechmaticsError(f"Unexpected Speechmatics response: {type(data).__name__}")
        return data
