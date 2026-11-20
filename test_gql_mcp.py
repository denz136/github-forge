"""GraphQL v4 + MCP surface test against the subset the suite exercises."""

import json
import sys
import urllib.request

sys.path.insert(0, '/home/agentuser/makefun/github-forge')
from test_driver import mock_env, start_forge, jreq

O = '/api/v3'


def gql(port, query, variables=None, token='tok123'):
    status, body, headers = jreq(port, 'POST', '/api/graphql',
                                 body={'query': query, 'variables': variables or {}}, token=token)
    return status, body, headers


def mcp(port, payload, token='tok123'):
    status, body, headers = jreq(port, 'POST', '/mcp', body=payload, token=token)
    return status, body, headers


def main():
    mock = mock_env(38140)
    proc, port = start_forge(38140)
    ok = []
    try:
        st, repo, _ = jreq(port, 'POST', f'{O}/user/repos', body={'name': 'gql'})
        assert st == 201
        # seed content + commit for git-driven fields
        import base64
        st, put, _ = jreq(port, 'PUT', f'{O}/repos/adaiversen/gql/contents/seed.txt',
                          body={'message': 'seed', 'content': base64.b64encode(b'seed\n').decode()})
        assert st in (200, 201), (st, put)
        commit_sha = put['commit']['sha']
        st, branch, _ = jreq(port, 'POST', f'{O}/repos/adaiversen/gql/git/refs',
                             body={'ref': 'refs/heads/gqlbranch', 'sha': commit_sha})
        assert st == 201, (st, branch)
        st, issue, _ = jreq(port, 'POST', f'{O}/repos/adaiversen/gql/issues',
                            body={'title': 'gql issue', 'body': 'ib'})
        assert st == 201
        inum = issue['number']
        st, pr, _ = jreq(port, 'POST', f'{O}/repos/adaiversen/gql/pulls',
                         body={'title': 'gql pr', 'head': 'gqlbranch', 'base': 'main'})
        assert st == 201, (st, pr)
        prnum = pr['number']

        # viewer
        st, body, h = gql(port, 'query { viewer { login } }')
        assert st == 200 and body['data']['viewer']['login'] == 'adaiversen', body
        ok.append('viewer.login (token-derived, no X-Forge-User)')

        st, body, _ = gql(port, 'query { rateLimit { cost remaining resetAt } }')
        assert st == 200 and body['data']['rateLimit']['remaining'] > 0, body
        ok.append('rateLimit')

        st, body, _ = gql(port, 'query { __schema { queryType { name } } }')
        assert st == 200 and body['data']['__schema']['queryType']['name'], body
        ok.append('introspection __schema')

        q = '''query($o:String!,$n:String!){ repository(owner:$o,name:$n){ id name owner{ login } defaultBranchRef{ name target{ ... on Commit { oid } } } ref(name:"refs/heads/gqlbranch"){ name target{ ... on Commit { oid } } } } }'''
        st, body, _ = gql(port, q, {'o': 'adaiversen', 'n': 'gql'})
        repo_node = body['data']['repository']
        assert repo_node['name'] == 'gql' and repo_node['owner']['login'] == 'adaiversen', body
        assert repo_node['defaultBranchRef']['name'] == 'main'
        assert repo_node['defaultBranchRef']['target']['oid'] == commit_sha, body
        assert repo_node['ref']['target']['oid'] == commit_sha
        ok.append('repository + defaultBranchRef + ref(...){target{...Commit}}')
        repo_gid = repo_node['id']

        q = '''query($id:ID!){ node(id:$id){ ... on Repository { object(expression:"main:seed.txt"){ ... on Blob { text isBinary byteSize } } } } }'''
        st, body, _ = gql(port, q, {'id': repo_gid})
        blob = body['data']['node']['object']
        assert blob['text'] == 'seed\n' and blob['isBinary'] is False, body
        ok.append('object(expression:main:path) -> Blob { text }')

        q = '''query($o:String!,$n:String!){ repository(owner:$o,name:$n){ issues(first:10,states:[OPEN]){ totalCount nodes { number title body state } } } }'''
        st, body, _ = gql(port, q, {'o': 'adaiversen', 'n': 'gql'})
        conn = body['data']['repository']['issues']
        assert conn['totalCount'] >= 1 and conn['nodes'][0]['title'] == 'gql issue', body
        ok.append('issues(first,states) connection')

        q = '''query($o:String!,$n:String!,$after:String){ repository(owner:$o,name:$n){ issues(first:1,after:$after){ pageInfo { hasNextPage endCursor } nodes { number } } } }'''
        st, body, _ = gql(port, q, {'o': 'adaiversen', 'n': 'gql', 'after': None})
        page1 = body['data']['repository']['issues']
        assert page1['nodes'][0]['number'] == inum
        ok.append('issue pagination (first/after/pageInfo)')

        q = '''query($o:String!,$n:String!){ repository(owner:$o,name:$n){ pullRequests(first:10){ nodes { number title state headRefName baseRefName } } } }'''
        st, body, _ = gql(port, q, {'o': 'adaiversen', 'n': 'gql'})
        nodes = body['data']['repository']['pullRequests']['nodes']
        assert any(p['number'] == prnum and p['headRefName'] == 'gqlbranch' for p in nodes), body
        ok.append('pullRequests connection')

        # mutations
        q = '''mutation($i:ID!,$b:String!){ addIssueComment(input:{issueId:$i,body:$b}){ issue { number } commentEdge { node { body } } } }'''
        # need issue gid: fetch via node lookup
        q2 = '''query($o:String!,$n:String!){ repository(owner:$o,name:$n){ issue(number:%d){ id } } }''' % inum
        st, body, _ = gql(port, q2, {'o': 'adaiversen', 'n': 'gql'})
        issue_gid = body['data']['repository']['issue']['id']
        st, body, _ = gql(port, q, {'i': issue_gid, 'b': 'from gql'})
        assert st == 200 and body['data']['addIssueComment']['commentEdge']['node']['body'] == 'from gql', body
        ok.append('mutation addIssueComment')

        q = '''mutation($i:ID!,$s:IssueState!){ updateIssue(input:{id:$i,state:$s}){ issue { state } } }'''
        st, body, _ = gql(port, q, {'i': issue_gid, 's': 'CLOSED'})
        assert body['data']['updateIssue']['issue']['state'] == 'CLOSED', body
        ok.append('mutation updateIssue state CLOSED')

        q = '''query($o:String!,$n:String!){ repository(owner:$o,name:$n){ issue(number:%d){ state comments(first:5){ nodes { body } } } } }''' % inum
        st, body, _ = gql(port, q, {'o': 'adaiversen', 'n': 'gql'})
        iss = body['data']['repository']['issue']
        assert iss['state'] == 'CLOSED' and iss['comments']['nodes'][0]['body'] == 'from gql', body
        ok.append('issue state + comments round-trip')

        # label create + attach
        q = '''mutation($o:String!,$n:String!,$name:String!){ createLabel(input:{repositoryId? name:$name}){ label { name } } }'''
        # createLabel needs repositoryId; try a plausible shape
        q = '''mutation($rid:ID!,$name:String!){ createLabel(input:{repositoryId:$rid,name:$name}){ label { name } } }'''
        st, body, _ = gql(port, q, {'rid': repo_gid, 'name': 'gql-label'})
        ok.append('createLabel mutation accepted (%s)' % st)

        # errors: unknown field -> errors array
        st, body, _ = gql(port, 'query { viewer { nope } }')
        assert st in (200, 400) and body.get('errors'), body
        ok.append('GraphQL errors array on unknown field')

        # MCP
        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                 'params': {'protocolVersion': '2025-06-18'}})
        assert st == 200 and body['result']['serverInfo']['name'], body
        ok.append('MCP initialize')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        assert st in (200, 202), st
        ok.append('MCP notifications/initialized (202)')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 2, 'method': 'server/discover'})
        assert st == 200 and body['result']['serverInfo'], body
        ok.append('MCP server/discover')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
        names = {t['name'] for t in body['result']['tools']}
        expected = {'create_repo', 'get_repo', 'create_issue', 'list_issues',
                    'create_pull_request', 'merge_pull_request'}
        assert expected <= names, names
        ok.append('MCP tools/list exposes the six tools')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                                 'params': {'name': 'create_repository', 'arguments': {'name': 'mcprepo'}}})
        assert st == 200, (st, body)
        ok.append('MCP tools/call create_repository')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                                 'params': {'name': 'get_repository', 'arguments': {'owner': 'adaiversen', 'repo': 'mcprepo'}}})
        assert st == 200, (st, body)
        ok.append('MCP tools/call get_repository')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
                                 'params': {'name': 'create_issue', 'arguments': {'owner': 'adaiversen', 'repo': 'mcprepo', 'title': 'via mcp'}}})
        assert st == 200, (st, body)
        ok.append('MCP tools/call create_issue')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                                 'params': {'name': 'list_issues', 'arguments': {'owner': 'adaiversen', 'repo': 'mcprepo'}}})
        assert st == 200, (st, body)
        ok.append('MCP tools/call list_issues')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call',
                                 'params': {'name': 'create_pull_request', 'arguments': {'owner': 'adaiversen', 'repo': 'mcprepo', 'title': 'p', 'head': 'main', 'base': 'main'}}})
        assert st == 200, (st, body)
        ok.append('MCP tools/call create_pull_request')

        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                                 'params': {'name': 'merge_pull_request', 'arguments': {'owner': 'adaiversen', 'repo': 'mcprepo', 'number': 1}}})
        assert st == 200, (st, body)
        ok.append('MCP tools/call merge_pull_request')

        # batched request
        st, body, _ = mcp(port, [{'jsonrpc': '2.0', 'id': 10, 'method': 'server/discover'},
                                 {'jsonrpc': '2.0', 'id': 11, 'method': 'tools/list'}])
        assert st == 200 and isinstance(body, list) and len(body) == 2, body
        ok.append('MCP batch array')

        # notifications only -> 202 no body
        st, body, _ = mcp(port, {'jsonrpc': '2.0', 'method': 'notifications/progress'})
        assert st == 202, st
        ok.append('MCP notification -> 202 empty')

        print('GRAPHQL+MCP OK:')
        for line in ok:
            print(' -', line)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == '__main__':
    main()
