import json
import pytest
import httpx
from session_recall import config
from session_recall.inference import InferenceClient, InferenceEmbedder, InferenceReranker


def client_for(handler):
    client = InferenceClient('https://inference.example/v1', 'test-key')
    client._client = httpx.Client(base_url=client.base_url + '/', transport=httpx.MockTransport(handler))
    return client


def test_query_documents_batches_and_response_order():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == '/v1/embeddings'
        return httpx.Response(200, json={'data': [
            {'index': i, 'embedding': [float(i), 1.0]} for i in reversed(range(len(body['input'])))
        ]})
    embedder = InferenceEmbedder('embedder', 2, client_for(handler))
    assert len(embedder.embed_documents(['document'] * 65)) == 65
    assert embedder.embed_query('query') == [0.0, 1.0]
    assert [c['input_type'] for c in calls] == ['document', 'document', 'query']
    assert [len(c['input']) for c in calls] == [64, 1, 1]
    assert all('dimensions' not in c for c in calls)


@pytest.mark.parametrize('data', [
    [{'index': 0, 'embedding': [1]}],
    [{'index': 1, 'embedding': [1, 2]}],
    [],
])
def test_embedding_rejects_wrong_dimensions_or_missing_results(data):
    e = InferenceEmbedder('embedder', 2, client_for(lambda r: httpx.Response(200,json={'data':data})))
    with pytest.raises(ValueError):
        e.embed_query('query')


def test_rerank_contract():
    def handler(request):
        assert request.url.path == '/v1/rerank'
        assert json.loads(request.content) == {
            'model':'reranker','query':'q','documents':['a','b'],'top_n':2,
            'truncate_prompt_tokens':8192,
        }
        return httpx.Response(200, json={'results':[
            {'index':0,'relevance_score':0.1}, {'index':1,'relevance_score':0.9},
        ]})
    r = InferenceReranker('reranker',client_for(handler))
    assert r.rerank('q',['a','b'],3) == [(1,0.9),(0,0.1)]
    assert r.rerank('q',[],3) == []


@pytest.mark.parametrize('results', [
    [{'index':2,'relevance_score':0.8}],
    [{'index':0,'relevance_score':0.8},{'index':0,'relevance_score':0.2}],
    [],
])
def test_rerank_rejects_invalid_results(results):
    r = InferenceReranker('reranker',client_for(lambda r: httpx.Response(200,json={'results':results})))
    with pytest.raises(ValueError):
        r.rerank('q',['a','b'],2)


def test_retry_and_no_response_body_in_errors(monkeypatch):
    calls = []
    monkeypatch.setattr('session_recall.inference.time.sleep',lambda _:None)
    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls)<3 else 200,json={'data':[]})
    assert client_for(handler).post('embeddings',{}) == {'data':[]}
    assert len(calls)==3
    c = client_for(lambda r:httpx.Response(401,text='sensitive response'))
    with pytest.raises(RuntimeError,match='HTTP 401') as exc:
        c.post('embeddings',{})
    assert 'sensitive' not in str(exc.value)


def test_requires_dedicated_key_and_https(monkeypatch):
    monkeypatch.delenv('INFERENCE_API_KEY',raising=False)
    monkeypatch.setenv('OPENAI_API_KEY','unrelated')
    with pytest.raises(ValueError,match='INFERENCE_API_KEY'):
        InferenceClient('https://inference.example/v1').client
    with pytest.raises(ValueError,match='HTTPS'):
        InferenceClient('http://inference.example/v1','test').client


def test_preset_and_fingerprint(monkeypatch):
    settings = config.resolve_embed({'SESSION_RECALL_EMBED':'inference-api'})
    assert settings.provider == settings.rerank_provider == 'inference-api'
    assert settings.model == 'embedder' and settings.rerank_model == 'reranker'
    monkeypatch.setattr(config,'EMBED_PROVIDER','inference-api')
    before=config.embed_fingerprint()
    monkeypatch.setenv('SESSION_RECALL_EMBED_REVISION','new-backend')
    assert before != config.embed_fingerprint()


def test_base64_embeddings_decode_little_endian_float32():
    import base64, struct
    value=base64.b64encode(struct.pack('<2f',0.25,-0.5)).decode()
    c=client_for(lambda r:httpx.Response(200,json={'data':[{'index':0,'embedding':value}]}))
    assert InferenceEmbedder('embedder',2,c).embed_query('query') == [0.25,-0.5]
