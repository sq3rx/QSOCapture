import urllib.request, json, os

out = []
try:
    q = urllib.request.urlopen('http://127.0.0.1:8000/api/qsos?limit=3', timeout=5).read().decode()
    c = urllib.request.urlopen('http://127.0.0.1:8000/api/contests', timeout=5).read().decode()
    qj = json.loads(q)
    cj = json.loads(c)
    out.append("QSOS count=" + str(qj.get('count')))
    out.append("first qso contest=" + str((qj.get('qsos') or [{}])[0].get('contest')))
    out.append("CONTESTS=" + str(cj.get('contests')))
except Exception as e:
    out.append("ERROR: " + repr(e))

with open(r'c:\Users\przem\Documents\Python\QSOCapture\_api_result.txt', 'w', encoding='utf-8') as f:
    f.write("\n".join(out))
print("WROTE")