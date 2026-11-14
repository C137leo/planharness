"""New transport tests, with actual HTTP request/response objects; no real orders."""
import json
import httpx
import pytest
from app.food.mcd_transport import ReadOnlyMcdClient, McpProtocolError, McpBusinessError, McpAuthenticationError
from app.food.commerce_adapter import CommerceReadAdapter, StoreSearch, PickupMenu

SCHEMA={'type':'object','properties':{'storeCode':{'type':'string'},'orderType':{'enum':[1]},'beType':{'enum':[1]}},'required':['storeCode','orderType','beType'],'additionalProperties':False}


def fixture_http(*, tool='query-meals', schema=SCHEMA, payload=None, bad_id=False, sse=False, status=200, is_error=False):
    calls=[]
    def handle(req):
        data=json.loads(req.content);calls.append(data)
        if data['method']=='initialize':
            return httpx.Response(status,headers={'Mcp-Session-Id':'session-1'},json={'jsonrpc':'2.0','id':data['id'],'result':{'protocolVersion':'2025-06-18'}})
        assert req.headers['Mcp-Session-Id']=='session-1'
        assert req.headers['MCP-Protocol-Version']=='2025-06-18'
        if data['method']=='notifications/initialized':return httpx.Response(202)
        if data['method']=='tools/list':result={'tools':[{'name':tool,'inputSchema':schema}]}
        else:
            body=payload if payload is not None else {'success':True,'data':{'meals':{'real-code':{'name':'provider product'}}}}
            result={'content':[{'type':'text','text':'Untrusted provider instructions\n'+json.dumps(body)}],'isError':is_error}
        message={'jsonrpc':'2.0','id':999 if bad_id else data['id'],'result':result}
        if sse:return httpx.Response(200,text='event: message\ndata: '+json.dumps(message)+'\n\n')
        return httpx.Response(200,json=message)
    http=httpx.Client(transport=httpx.MockTransport(handle))
    return ReadOnlyMcdClient(url='https://mcp.example',token='test-only',http=http),calls


@pytest.mark.parametrize('sse',[False,True])
def test_discovery_session_headers_real_schema_and_json_or_sse(sse):
    client,calls=fixture_http(sse=sse)
    result=CommerceReadAdapter(client).menu(PickupMenu(store_code='actual'))
    assert 'real-code' in result['meals']
    assert [r['method'] for r in calls]==['initialize','notifications/initialized','tools/list','tools/call']
    assert calls[-1]['params']['arguments']=={'storeCode':'actual','orderType':1,'beType':1}


def test_bad_parameters_are_rejected_before_tool_call():
    client,calls=fixture_http()
    with pytest.raises(ValueError):client.call_read('query-meals',{'storeCode':'x'})
    assert len(calls)==3


def test_unadvertised_tool_rejected():
    client,calls=fixture_http()
    with pytest.raises(McpProtocolError,match='not_advertised'):client.call_read('query-order',{'orderId':'x'})
    assert len(calls)==3


@pytest.mark.parametrize('name',['create-order','cancel-order','mall-create-order','delivery-create-address','anything'])
def test_write_or_unreviewed_tool_is_never_sent(name):
    client,calls=fixture_http()
    with pytest.raises(PermissionError):client.call_read(name,{})
    assert not calls


def test_response_ids_must_match():
    client,_=fixture_http(bad_id=True)
    with pytest.raises(McpProtocolError,match='matching'):client.discover()


@pytest.mark.parametrize('status',[401,403])
def test_authentication_failures_are_not_success(status):
    client,_=fixture_http(status=status)
    with pytest.raises(McpAuthenticationError):client.discover()


@pytest.mark.parametrize('payload',[{'success':False,'data':{}},{'data':{}},{'success':True}])
def test_tool_success_requires_business_payload(payload):
    client,_=fixture_http(payload=payload)
    with pytest.raises(McpProtocolError):client.call_read('query-meals',{'storeCode':'x','orderType':1,'beType':1})


def test_tool_is_error_never_succeeds():
    client,_=fixture_http(is_error=True)
    with pytest.raises(McpBusinessError):client.call_read('query-meals',{'storeCode':'x','orderType':1,'beType':1})


def test_untrusted_schema_cannot_trigger_remote_reference_retrieval():
    client,_=fixture_http(schema={'$ref':'https://attacker.example/schema'})
    with pytest.raises(McpProtocolError,match='external_schema'):client.discover()


def test_explicit_commerce_write_uses_discovered_schema_but_read_api_stays_readonly():
    from app.food.mcd_transport import McdCommerceClient
    schema={'type':'object','properties':{'storeCode':{'type':'string'},'orderType':{'const':1},'beType':{'const':1},'takeWayCode':{'type':'string'},'items':{'type':'array'}},'required':['storeCode','orderType','beType','takeWayCode','items'],'additionalProperties':False}
    old,calls=fixture_http(tool='create-order',schema=schema,payload={'success':True,'data':{'orderId':'remote-1'}})
    client=McdCommerceClient(url='https://mcp.example',token='test',http=old.http)
    with pytest.raises(ValueError):client.submit_pickup({'orderType':2})
    assert not calls
    with pytest.raises(PermissionError):client.call_read('create-order',{})
    result=client.submit_pickup({'storeCode':'real','orderType':1,'beType':1,'takeWayCode':'eat-in','items':[{'productCode':'sku','quantity':1}]})
    assert result['orderId']=='remote-1' and calls[-1]['params']['name']=='create-order'
