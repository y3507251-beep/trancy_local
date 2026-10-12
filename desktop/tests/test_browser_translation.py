"""Controlled bridge/identity/security tests; no remote request or ASR is implied."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trancy_desktop.browser_bridge import BrowserBridge, extension_id, media_url_allowed, normalize_video
from trancy_desktop.translation import TranslationClient, TranslationError, detect_target
from trancy_desktop.media_download import MediaDownloader
from trancy_desktop.media_download_worker import extractor_urls


VIDEO = {"url": "https://www.bilibili.com/video/BV1xx411c7mD/?p=2", "platform": "bilibili",
         "video_id": "BV1xx411c7mD", "page": 2, "cid": "12345", "title": "Test video", "position": 5,
         "duration": 100, "paused": False, "rate": 1,
         "audio_urls": ["https://upos-sz-mirror.bilivideo.com/upgcxcode/45/23/12345/12345-1-30280.m4s?secret=ephemeral"]}


class VideoIdentityTests(unittest.TestCase):
    def test_bili_part_cid_and_media_are_bound(self):
        result = normalize_video(VIDEO)
        self.assertEqual(result["key"], "bilibili:BV1xx411c7mD:2:12345")
        self.assertEqual(len(result["audio_urls"]), 1)
        self.assertEqual(normalize_video(dict(VIDEO, cid="99999"))["audio_urls"], [])
        with self.assertRaises(ValueError):
            normalize_video(dict(VIDEO, page=1))
        with self.assertRaises(ValueError):
            normalize_video(dict(VIDEO, video_id="BV2xx411c7mD"))

    def test_only_video_pages_and_media_hosts_are_accepted(self):
        for url in ("https://127.0.0.1/admin", "file:///c:/test.mp4", "https://www.bilibili.com@evil.test/video/BV1xx411c7mD"):
            with self.assertRaises(ValueError):
                normalize_video(dict(VIDEO, url=url))
        self.assertFalse(media_url_allowed("https://googlevideo.com.evil.test/audio", "youtube"))
        self.assertFalse(media_url_allowed("https://user:pass@googlevideo.com/audio", "youtube"))
        self.assertFalse(media_url_allowed("https://googlevideo.com:444/audio", "youtube"))
        self.assertTrue(media_url_allowed("https://rr1.googlevideo.com/videoplayback", "youtube"))

    def test_youtube_canonical_identity(self):
        source = {"url": "https://www.youtube.com/shorts/BaW_jenozKc?feature=test", "video_id": "BaW_jenozKc", "platform": "youtube"}
        video = normalize_video(source)
        self.assertEqual(video["key"], "youtube:BaW_jenozKc")
        self.assertEqual(video["url"], "https://www.youtube.com/watch?v=BaW_jenozKc")
        with self.assertRaises(ValueError):
            normalize_video(dict(source, video_id="xxxxxxxxxxx"))

    def test_translation_direction_and_empty_batch(self):
        self.assertEqual(detect_target("这是一个 English 术语。", "en"), ("zh-CN", "en"))
        self.assertEqual(detect_target("This is a test.", "zh"), ("en", "zh-CN"))
        self.assertEqual(detect_target("123", "Chinese"), ("zh-CN", "en"))
        self.assertEqual(TranslationClient().translate([], "en", "zh-CN"), [])
        with self.assertRaises(TranslationError):
            TranslationClient("plugin").translate(["test"], "en", "zh-CN")

    def test_active_live_video_is_reported_but_not_downloaded_as_a_file(self):
        video = {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "video_id": "jNQXAC9IVRw",
                 "platform": "youtube", "is_live": True}
        self.assertTrue(normalize_video(video)["is_live"])
        downloader = MediaDownloader()
        errors = []
        downloader.failed.connect(errors.append)
        downloader.download(video, "unused-live-video-download-directory")
        self.assertFalse(downloader.running)
        self.assertTrue(errors and "直播" in errors[0])
        class FakeYoutubeDL:
            def __init__(self, _): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def extract_info(self, *_args, **_kwargs):
                return {"id": "jNQXAC9IVRw", "is_live": True, "formats": []}
        with patch.dict(sys.modules, {"yt_dlp": type("FakeModule", (), {"YoutubeDL": FakeYoutubeDL})}):
            with self.assertRaisesRegex(RuntimeError, "直播"):
                extractor_urls(video)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = BrowserBridge()
        self.bridge.start(0)
        self.client = "client-test-1234567890"
        self.headers = {"Content-Type": "application/json", "Origin": "chrome-extension://" + extension_id(),
                        "X-Trancy-Desktop": "1", "X-Trancy-Extension": extension_id()}
        self.session = self.post("/v1/hello", {})["session"]
        self.headers["X-Trancy-Session"] = self.session

    def tearDown(self):
        self.bridge.stop()

    def post(self, path, body, headers=None):
        request = Request(f"http://127.0.0.1:{self.bridge.port}" + path,
                          json.dumps(dict(body, client_id=self.client)).encode(),
                          headers=headers or self.headers, method="POST")
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_origin_session_and_unknown_endpoints(self):
        for headers in (dict(self.headers, Origin="https://evil.test"),
                        {k: v for k, v in self.headers.items() if k != "X-Trancy-Desktop"},
                        dict(self.headers, **{"X-Trancy-Session": "wrong"})):
            with self.assertRaises(HTTPError) as raised:
                self.post("/v1/poll", {}, headers)
            self.assertEqual(raised.exception.code, 403)
        with self.assertRaises(HTTPError) as raised:
            self.post("/download", {"url": "http://127.0.0.1/private", "path": "c:/secret"})
        self.assertEqual(raised.exception.code, 404)

    def test_tabs_sequences_and_closed_video(self):
        self.post("/v1/video", {"tab_id": 10, "seq": 2, "video": VIDEO})
        result = self.post("/v1/video", {"tab_id": 10, "seq": 1, "video": dict(VIDEO, position=1)})
        self.assertFalse(result["accepted"])
        self.assertEqual(self.bridge.latest()["position"], 5)
        self.post("/v1/video", {"tab_id": 11, "seq": 1, "video": dict(VIDEO, position=25)})
        self.assertEqual(len(self.bridge.videos()), 2)
        self.post("/v1/closed", {"tab_id": 10, "seq": 3})
        self.assertEqual([v["tab_id"] for v in self.bridge.videos()], [11])
        self.post("/v1/video", {"tab_id": 10, "seq": 2, "video": VIDEO})
        self.assertEqual(len(self.bridge.videos()), 1)

    def test_stale_video_disappears(self):
        self.post("/v1/video", {"tab_id": 10, "seq": 1, "video": VIDEO})
        for item in self.bridge._videos.values():
            item["_seen"] -= 10
        self.assertEqual(self.bridge.videos(), [])

    def test_roundtrip_translation_does_not_transfer_configuration(self):
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.bridge.translate, ["Hello"], "en", "zh-CN", 3)
            jobs = []
            for _ in range(20):
                jobs = self.post("/v1/poll", {})["jobs"]
                if jobs:
                    break
                time.sleep(.01)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(set(jobs[0]), {"id", "texts", "source", "target"})
            self.post("/v1/result", {"id": jobs[0]["id"], "translations": ["你好"]})
            self.assertEqual(future.result(), ["你好"])

    def test_bad_result_fails_without_echoing_secret_error(self):
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.bridge.translate, ["Hello"], "en", "zh-CN", 3)
            jobs = []
            for _ in range(20):
                jobs = self.post("/v1/poll", {})["jobs"]
                if jobs:
                    break
                time.sleep(.01)
            self.post("/v1/result", {"id": jobs[0]["id"], "error": "secret=not-for-logs"})
            with self.assertRaises(TranslationError) as raised:
                future.result()
            self.assertNotIn("secret", str(raised.exception))


class ExtensionBridgeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the extension sandbox test")
    def test_main_observer_rejects_stale_player_and_part_audio(self):
        script = r'''
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const code=fs.readFileSync('assets/desktop-bridge-page.js','utf8');
function setup(href) {
  let report, messages=[], release;
  const media={currentTime:4,duration:19,paused:false,ended:false,readyState:4,playbackRate:1};
  class Xhr {open(){} addEventListener(){}}
  const ctx={URL,location:new URL(href),performance:{timeOrigin:100},XMLHttpRequest:Xhr,
    setInterval:fn=>{report=fn;}, document:{visibilityState:'visible',title:'Example',querySelector:()=>media},
    addEventListener:()=>{},postMessage:m=>messages.push(m),
    fetch:()=>new Promise(resolve=>{release=payload=>resolve({clone:()=>({json:async()=>payload})});})};
  ctx.window=ctx;ctx.top=ctx;vm.createContext(ctx);
  return {ctx,media,messages,start:()=>vm.runInContext(code,ctx),report:()=>report(),release:p=>release(p)};
}
(async()=>{
  const bili=setup('https://www.bilibili.com/video/BV1PPQzByEAC/?p=1');
  bili.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',cid:111,aid:1,title:'Bili',pages:[{page:1,cid:111},{page:2,cid:222}]}};
  const audio=cid=>({code:0,data:{dash:{audio:[{baseUrl:`https://cdn.bilivideo.com/${cid}/${cid}-1-30280.m4s?sign=test`} ]}}});
  bili.ctx.__playinfo__=audio(111);bili.start();
  assert.equal(bili.messages.at(-1).video.cid,'111');
  assert.equal(bili.messages.at(-1).video.audio_urls.length,1);
  const old=bili.ctx.fetch('https://api.bilibili.com/x/player/playurl?bvid=BV1PPQzByEAC&cid=111');
  bili.ctx.location=new URL('https://www.bilibili.com/video/BV1PPQzByEAC/?p=2');
  bili.release(audio(111));await old;await new Promise(setImmediate);bili.report();
  assert.equal(bili.messages.at(-1).video.cid,'222');
  assert.equal(bili.messages.at(-1).video.audio_urls.length,0);
  const next=bili.ctx.fetch('https://api.bilibili.com/x/player/playurl?bvid=BV1PPQzByEAC&cid=222');
  bili.release(audio(222));await next;await new Promise(setImmediate);bili.report();
  assert.equal(bili.messages.at(-1).video.audio_urls.length,1);
  const yt=setup('https://www.youtube.com/watch?v=jNQXAC9IVRw');
  let response={videoDetails:{videoId:'jNQXAC9IVRw',title:'Example',isLive:true},streamingData:{adaptiveFormats:[]}};
  const player={getPlayerResponse:()=>response,querySelector:()=>yt.media,classList:{contains:x=>x==='ad-showing'}};
  yt.ctx.document.querySelector=()=>player;yt.start();
  assert.equal(yt.messages.at(-1).video.ad_playing,true);
  assert.equal(yt.messages.at(-1).video.is_live,true);
  response={videoDetails:{videoId:'xxxxxxxxxxx'}};yt.report();
  assert.equal(yt.messages.at(-1).video,null);
  console.log('MAIN observer controlled test passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the extension sandbox test")
    def test_background_reuses_selected_engine_and_isolates_tabs(self):
        # This executes the real independent background bridge in a controlled
        # Chrome API sandbox. It is not a claim of live Chrome integration.
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
(async () => {
  let listener, timer, jobs = [], requests = [], translated;
  const selectedEngine = {_id:'user-selected-engine',type:'custom',key:'private-provider-key'};
  const context = {URL, AbortSignal, crypto:require('node:crypto').webcrypto,
    setTimeout: fn => {timer=fn;return 1;}, clearTimeout:()=>{},
    chrome:{runtime:{id:'extension-test',getPlatformInfo:async()=>({}),onMessage:{addListener:fn=>listener=fn}},
      tabs:{onRemoved:{addListener:()=>{}},onUpdated:{addListener:()=>{}}},
      alarms:{create:()=>{},onAlarm:{addListener:()=>{}}}},
    fetch:async (url, options)=>{
      const body=JSON.parse(options.body); requests.push({url,body});
      return {ok:true,json:async()=>url.endsWith('/hello')?{session:'test-session'}:
        url.endsWith('/poll')?{jobs:jobs.splice(0)}:{accepted:true}};
    }};
  context.self=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('assets/desktop-bridge-background.js','utf8'),context);
  context.TrancyDesktopBridge.start({getState:async()=>({translatorService:{subtitle:selectedEngine},user:{token:'private-user-token'}}),
    translate:async args=>{translated=args;return args.texts.map(t=>({message:'ok',translation:'translated '+t}));}});
  await new Promise(setImmediate);
  const video={platform:'youtube',video_id:'jNQXAC9IVRw',url:'https://www.youtube.com/watch?v=jNQXAC9IVRw',position:5,duration:19,paused:false,page:1,audio_urls:[],is_live:true};
  const sender=tab=>({id:'extension-test',frameId:0,tab:{id:tab,url:video.url,active:true}});
  function report(tab, seq, data=video) {listener({kind:'trancy-desktop-report-v1',href:video.url,document_start:100,sequence:seq,video:data},sender(tab));}
  report(10,2); report(11,1); report(10,1,{...video,position:999});
  await timer();
  let sent=requests.filter(r=>r.url.endsWith('/video'));
  assert.equal(sent.length,2); assert.equal(sent.find(r=>r.body.tab_id===10).body.video.position,5);
  assert.equal(sent.find(r=>r.body.tab_id===10).body.video.is_live,true);
  report(10,3,null); await new Promise(setImmediate); report(10,2); await timer();
  assert.equal(requests.filter(r=>r.url.endsWith('/video')&&r.body.tab_id===10).length,1);
  jobs.push({id:'job-1',texts:['Hello'],source:'en',target:'zh-CN'});
  await timer(); await new Promise(setImmediate);
  assert.equal(translated.engine,selectedEngine); assert.equal(translated.ut,'private-user-token');
  const result=requests.find(r=>r.url.endsWith('/result'));
  assert.equal(result.body.translations[0],'translated Hello');
  assert.ok(!JSON.stringify(requests).includes('private-provider-key'));
  assert.ok(!JSON.stringify(requests).includes('private-user-token'));
  console.log('extension background controlled test passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
