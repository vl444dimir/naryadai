"""The acceptance harness must reject a valid-JSON model that accepts everything."""
import asyncio
import json

import httpx

from app.services.review import ProviderOptions
from scripts.verify_model import verify


def test_model_verifier_rejects_semantically_wrong_but_well_formed_provider(monkeypatch):
    original_client = httpx.AsyncClient
    calls = []

    def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(payload)
        if "facts" in payload:
            output = {"text": "Выдано 10 нарядов, выполнено 7.", "evidence": ["counts.issued", "counts.completed"]}
        else:
            output = {"verdict": "accepted", "score": 95, "explanation": "Все работы якобы соответствуют проблеме.",
                      "good": [], "improve": [], "findings": [], "needs_master_review": True}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]})

    def client_factory(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    result = asyncio.run(verify(ProviderOptions(enabled=True, base_url="https://model.example/v1", model="test")))
    assert len(calls) == 7
    assert result["synthetic_only"] and result["summary_passed"]
    assert all(row["live_model"] for row in result["cases"])
    assert not result["passed"]
    semantic = [row for row in result["cases"] if row["tests_model_semantics"]]
    assert len(semantic) == 3 and all(not row["passed"] for row in semantic)
