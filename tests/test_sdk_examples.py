"""Installed optional Gemini SDK examples, synthetic HTTP only; no provider calls."""
import asyncio
import json
import httpx
import pytest
from langsmith import tracing_context


def test_installed_gemini_sdk_invoke_and_stream_wire_examples(monkeypatch):
    sdk=pytest.importorskip('langchain_google_genai')
    monkeypatch.setenv('GOOGLE_API_KEY','fabricated-key');monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    for key in ['LANGCHAIN_TRACING_V2','LANGCHAIN_TRACING','LANGSMITH_TRACING']:monkeypatch.setenv(key,'false')
    for key in ['LANGSMITH_API_KEY','LANGCHAIN_API_KEY']:monkeypatch.delenv(key,raising=False)
    requests=[];prompt='직접 작성한 합성 SDK 예제';usage={'promptTokenCount':7,'candidatesTokenCount':3,'totalTokenCount':10}
    def response(text,final=False):
        candidate={'content':{'role':'model','parts':[{'text':text}]}}
        result={'candidates':[candidate]}
        if final:candidate['finishReason']='STOP';result['usageMetadata']=usage
        return result
    def handler(request):
        body=json.loads(request.content);assert body['contents']==[{'parts':[{'text':prompt}],'role':'user'}]
        assert body['generationConfig']['maxOutputTokens']==32 and request.headers['x-goog-api-key']=='fabricated-key'
        requests.append(request)
        if request.url.path.endswith(':generateContent'):return httpx.Response(200,json=response('모의 응답',True))
        assert request.url.path.endswith(':streamGenerateContent') and request.url.params['alt']=='sse'
        payload=''.join('data: '+json.dumps(v,ensure_ascii=False)+'\n\n' for v in [response('모의 '),response('응답',True)])
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=payload.encode())
    model=sdk.ChatGoogleGenerativeAI(model='gemini-2.5-flash',api_key='fabricated-key',vertexai=False,max_output_tokens=32,
        client_args={'transport':httpx.MockTransport(handler),'trust_env':False})
    try:
        with tracing_context(enabled=False):
            invoked=model.invoke(prompt,http_options={'retry_options':{'attempts':1}},config={'callbacks':[]})
            chunks=list(model.stream(prompt,http_options={'retry_options':{'attempts':1}},config={'callbacks':[]}))
        merged=chunks[0]
        for chunk in chunks[1:]:merged=merged+chunk
        assert str(invoked.text)==str(merged.text)=='모의 응답' and len(requests)==2
        for result in [invoked,merged]:
            assert result.usage_metadata['input_tokens']==7 and result.usage_metadata['output_tokens']==3 and result.usage_metadata['total_tokens']==10
            assert result.response_metadata['finish_reason']=='STOP'
    finally:
        asyncio.run(model.client.aio.aclose());model.client.close()
