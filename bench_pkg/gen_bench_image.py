from PIL import Image, ImageDraw, ImageFont
import glob
W,H=640,480
img=Image.new('RGB',(W,H),'white'); d=ImageDraw.Draw(img)
d.ellipse((60,120,220,280),fill='red')
d.rectangle((380,120,540,280),fill='blue')
d.polygon([(320,300),(240,420),(400,420)],fill='green')
allf=glob.glob('/usr/share/fonts/**/*.tt[cf]',recursive=True)
cjk=[c for c in allf if any(k in c.lower() for k in ('cjk','wqy','droidsansfallback','sourcehan','noto'))]
print('CJK fonts:', cjk[:5])
def load(size, prefs):
    for c in prefs+allf:
        try: return ImageFont.truetype(c,size)
        except Exception: pass
    return ImageFont.load_default()
d.text((40,30),'ATRI BENCH 2026',fill='black',font=load(46,cjk))
d.text((40,432),'高性能测试',fill='purple',font=load(34,cjk))
img.save('bench_test.png'); print('saved', img.size)
