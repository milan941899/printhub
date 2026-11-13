# Mini-Spoolman für Tests
import json, http.server, itertools, re
random_seed = 7
import random
random.seed(random_seed)
V=[{"id":1,"name":"Bambu Lab","empty_spool_weight":250},{"id":2,"name":"JAYO","empty_spool_weight":150},
   {"id":3,"name":"Sunlu","empty_spool_weight":189},{"id":4,"name":"Polymaker","empty_spool_weight":140},{"id":5,"name":"eSUN","empty_spool_weight":220}]
def fil(i,v,name,mat,hexc,w=1000,price=None,et=None,bt=None,sw=None):
    return {"id":i,"name":name,"material":mat,"vendor":V[v-1],"color_hex":hexc,"weight":w,"spool_weight":sw,"price":price,
            "density":{"PLA":1.24,"PETG":1.27,"ABS":1.04,"TPU":1.21}.get(mat.split("+")[0].split("-")[0],1.24),"diameter":1.75,
            "settings_extruder_temp":et,"settings_bed_temp":bt}
F=[fil(1,1,"Matte Charcoal","PLA","36393F",1000,22.99,220,65,250),
   fil(2,1,"Jade White","PLA","FFFFFF",1000,19.99,220,65,250),
   fil(3,1,"Iron Gray Metallic","PLA","4A4A4C",1000,24.99,220,65,250),
   fil(4,1,"Matte Lemon Yellow","PLA","F7D959",1000,22.99,220,65,250),
   fil(5,2,"Black","PETG","1F2937",1100,15.5,240,75,150),
   fil(6,2,"Gray","PETG","9CA3AF",1100,15.5,240,75,150),
   fil(7,2,"Brick Red","PETG","9B3A2E",1100,15.5,240,75,150),
   fil(8,2,"Electric Blue","PETG","2B3BFF",1100,15.5,240,75,150),
   fil(9,3,"Black","PLA+","000000",1000,13.99,215,60,189),
   fil(10,3,"White","PLA+","F5F5F5",1000,13.99,215,60,189),
   fil(11,4,"Panchroma Matte Fossil Grey","PLA","8A8C94",1000,21.99,210,55,145),
   fil(12,5,"Fire Engine Red","PETG","C8102E",1000,18.5,235,75,220),
   fil(13,5,"Natural","TPU","E8E4D8",1000,27.9,225,45,220)]
S=[]
nid=itertools.count(1)
def mk(fid,used=0,loc="Lager",first=None,arch=False,lot=None):
    f=next(x for x in F if x["id"]==fid); i=next(nid)
    S.append({"id":i,"filament":f,"initial_weight":f["weight"],"used_weight":used,"remaining_weight":f["weight"]-used,"location":loc,
              "first_used":first,"last_used":first,"archived":arch,"price":f["price"],"lot_nr":lot})
mk(1,383,"A1 – AMS Slot 1","2026-10-28T10:00:00",lot="921059215720"); mk(1)
mk(2,612,"A1 – AMS Slot 2","2026-10-12T09:00:00"); mk(2); mk(2)
mk(3,0,"A1 – AMS Slot 3","2026-11-10T18:00:00")
mk(4,140,"A1 – AMS Slot 4","2026-11-02T18:00:00")
mk(5,410,"Voron – Extruder","2026-11-05T08:00:00",lot="04260702AJ"); mk(5,lot="04260702AJ"); mk(5,lot="04260703AJ")
mk(6,0,lot="04460705AJ"); mk(6,lot="04460705AJ"); mk(6,980,"Lager","2026-09-01T10:00:00")
mk(7,lot="04260702AJ"); mk(8,lot="04360703AJ")
mk(9,900,"Lager","2026-08-14T10:00:00"); mk(9)
mk(10,950,"Lager","2026-10-02T10:00:00")
mk(11,250,"Lager","2026-10-21T10:00:00",lot="A7233121083")
mk(12)
mk(13,320,"Trockenbox","2026-09-20T10:00:00")
mk(9,1000,"Lager","2026-06-01T10:00:00",arch=True); mk(5,1100,"Lager","2026-07-01T10:00:00",arch=True)
EXT=[]
def ext(man,name,mat,hexc,et,bt,sw,w=1000):
    EXT.append({"id":f"{man}_{mat}_{name}".lower().replace(" ","_"),"manufacturer":man,"name":name,"material":mat,"color_hex":hexc,
                "extruder_temp":et,"bed_temp":bt,"spool_weight":sw,"density":1.24,"diameter":1.75,"weight":w})
for n,h in [("Matte Charcoal","36393F"),("Matte Lemon Yellow","F7D959"),("Matte Ivory White","FFFFF0"),("Matte Grass Green","61C680"),
            ("Jade White","FFFFFF"),("Black","000000"),("Gray","8E9089"),("Red","C12E1F"),("Blue","0A2989"),("Iron Gray Metallic","4A4A4C")]:
    ext("Bambu Lab",n,"PLA",h,220,65,250)
for n,h in [("Brick Red","9B3A2E"),("Electric Blue","2B3BFF"),("Grass Green","3FA34D"),("Black","1F2937"),("White","FFFFFF"),("Vibrant Orange","F57C00")]:
    ext("JAYO",n,"PETG",h,240,75,150,1100)
for n,h in [("Black","000000"),("White","F5F5F5"),("Grey","8A8A8A")]:
    ext("Sunlu",n,"PLA+",h,215,60,189)
for n,h in [("Panchroma Matte Fossil Grey","8A8C94"),("Panchroma Matte Earth Brown","6B4F3A"),("PolyLite PLA Pro Blue","0F5EA0")]:
    ext("Polymaker",n,"PLA",h,210,55,145)
JAYOX=[]; POLY=EXT
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _j(self,d,code=200):
        b=json.dumps(d).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))) or b"{}")
    def do_GET(self):
        p=self.path.split("?")[0]
        if p=="/api/v1/info": return self._j({"version":"0.22.1"})
        if p=="/api/v1/external/filament": return self._j([
            {"id":"sunlu_pla+_black_1000_175_n","manufacturer":"Sunlu","name":"PLA+ Black","material":"PLA+","density":1.24,"weight":1000,"spool_weight":160,"diameter":1.75,"color_hex":"000000","extruder_temp":215,"bed_temp":60,"finish":None},
            {"id":"sunlu_pla+_white_1000_175_n","manufacturer":"Sunlu","name":"PLA+ White","material":"PLA+","density":1.24,"weight":1000,"spool_weight":160,"diameter":1.75,"color_hex":"FFFFFF","extruder_temp":215,"bed_temp":60},
            {"id":"elegoo_petg_blue","manufacturer":"Elegoo","name":"Rapid PETG Blue","material":"PETG","density":1.27,"weight":1000,"spool_weight":170,"diameter":1.75,"color_hex":"1E5BD8","extruder_temp":240,"bed_temp":70},
            {"id":"polymaker_silk","manufacturer":"Polymaker","name":"PolySonic Silk Rainbow","material":"PLA","density":1.2,"weight":1000,"diameter":1.75,"color_hexes":["FF0000","00FF00"],"extruder_temp":220,"bed_temp":60,"finish":"glossy"}]+EXT)
        if p=="/api/v1/vendor": return self._j(V)
        if p=="/api/v1/filament": return self._j(F)
        if p=="/api/v1/spool": return self._j([s for s in S if (not s["archived"]) or "allow_archived=true" in self.path])
        m=re.match(r"/api/v1/spool/(\d+)$",p)
        if m: return self._j(next(s for s in S if s["id"]==int(m[1])))
        self._j({"detail":"nf"},404)
    def do_POST(self):
        b=self.body(); p=self.path
        if p=="/api/v1/vendor": v={"id":len(V)+1,**b}; V.append(v); return self._j(v)
        if p=="/api/v1/filament":
            f={"id":len(F)+1,**b,"vendor":next((v for v in V if v["id"]==b.get("vendor_id")),None)}; F.append(f); return self._j(f)
        if p=="/api/v1/spool":
            f=next(x for x in F if x["id"]==b["filament_id"]); i=next(nid); w=b.get("initial_weight") or f.get("weight")
            s={"id":i,"filament":f,"initial_weight":w,"used_weight":0,"remaining_weight":w,"location":b.get("location"),"first_used":None,"last_used":None,"archived":False,"price":b.get("price")}; S.append(s); return self._j(s)
        self._j({"detail":"nf"},404)
    def do_PATCH(self):
        b=self.body(); m=re.match(r"/api/v1/(spool|filament)/(\d+)$",self.path)
        L=S if m[1]=="spool" else F; o=next(x for x in L if x["id"]==int(m[2])); o.update(b)
        if m[1]=="spool":
            if "remaining_weight" in b: o["used_weight"]=o["initial_weight"]-b["remaining_weight"]
            o["remaining_weight"]=o["initial_weight"]-o["used_weight"]
        return self._j(o)
    def do_DELETE(self):
        m=re.match(r"/api/v1/spool/(\d+)$",self.path); i=int(m[1])
        before=len(S); S[:]=[x for x in S if x["id"]!=i]
        if len(S)==before: return self._j({"detail":"nf"},404)
        self._j({"ok":True})
    def do_PUT(self):
        b=self.body(); m=re.match(r"/api/v1/spool/(\d+)/use$",self.path); s=next(x for x in S if x["id"]==int(m[1])); s["used_weight"]+=b["use_weight"]; s["remaining_weight"]-=b["use_weight"]; self._j(s)
http.server.ThreadingHTTPServer(("127.0.0.1",7997),H).serve_forever()
