import os, unittest, threading, json
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import staff_auth, server

class AuthTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ, {'CS_STAFF_DOMAIN':'findme.com.ph','CS_STAFF_EMAILS':''})
        self.env.start()
        staff_auth._attempts.clear()
    def tearDown(self): self.env.stop()
    def test_exact_domain_and_confirmation_required(self):
        for bad in ('x@findme.com.ph.evil.com','x@evilfindme.com.ph','findme.com.ph','x@y@findme.com.ph'):
            self.assertFalse(staff_auth.approved(bad))
        self.assertTrue(staff_auth.approved('CS@Findme.com.ph'))
        self.assertIsNone(staff_auth.identity({'id':'1','email':'cs@findme.com.ph'}))
        self.assertEqual(staff_auth.identity({'id':'1','email':'cs@findme.com.ph','email_confirmed_at':'today'}),'cs@findme.com.ph')
    def test_cookie_is_verified_with_provider(self):
        with patch.object(staff_auth,'request',return_value={'id':'1','email':'cs@findme.com.ph','email_confirmed_at':'today'}) as req:
            self.assertEqual(staff_auth.current('__Host-cs-session=test-token'),'cs@findme.com.ph')
            req.assert_called_once_with('user',token='test-token')
        with patch.object(staff_auth,'request',side_effect=ValueError):
            self.assertIsNone(staff_auth.current('__Host-cs-session=forged'))
    def test_private_cookie_and_blocked_domain(self):
        with patch.object(staff_auth,'request') as req:
            with self.assertRaises(ValueError): staff_auth.login('x@other.com','secret')
            req.assert_not_called()
        with patch.object(staff_auth,'request',return_value={'user':{'id':'1','email':'cs@findme.com.ph','email_confirmed_at':'today'},'access_token':'abc','expires_in':9999}):
            cookie=staff_auth.login('cs@findme.com.ph','secret')
            for expected in ('Secure','HttpOnly','SameSite=Strict','Max-Age=3600'): self.assertIn(expected,cookie)
    def test_http_data_routes_and_actor_spoofing(self):
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        port=http.server_port
        worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
        try:
            with patch.object(server,'PORT',port), patch.object(server,'PUBLIC_ORIGIN','https://desk.example'), patch.object(staff_auth,'current',return_value=None):
                for path in ('/api/config','/api/tickets','/api/export','/api/ticket?id=1','/api/validation','/api/sheet-conflicts'):
                    with self.assertRaises(HTTPError) as err: urlopen(f'http://127.0.0.1:{port}{path}')
                    self.assertEqual(err.exception.code,401)
                with urlopen(f'http://127.0.0.1:{port}/') as response:
                    self.assertIn(b'Work email',response.read())
            with patch.object(server,'PORT',port), patch.object(server,'PUBLIC_ORIGIN','https://desk.example'), patch.object(staff_auth,'current',return_value='cs@findme.com.ph'), patch.object(server,'save',return_value={'ok':True}) as save:
                req=Request(f'http://127.0.0.1:{port}/api/ticket',data=json.dumps({'actor':'impersonated'}).encode(),headers={'Origin':'https://desk.example','X-CS-Token':server.TOKEN})
                with urlopen(req) as response: self.assertEqual(response.status,200)
                self.assertEqual(save.call_args.args[0]['actor'],'cs@findme.com.ph')
                req=Request(f'http://127.0.0.1:{port}/api/ticket',data=b'{}',headers={'Origin':'https://evil.example','X-CS-Token':server.TOKEN})
                with self.assertRaises(HTTPError) as err: urlopen(req)
                self.assertEqual(err.exception.code,403)
        finally: http.shutdown();http.server_close()

if __name__=='__main__': unittest.main()
