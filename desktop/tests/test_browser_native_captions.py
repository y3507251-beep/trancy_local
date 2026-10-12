"""Native text-track tests with fake sites; no live browser/audio/model is used."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop.browser_bridge import BrowserBridge, normalize_caption_cues


VIDEO = {"url": "https://www.bilibili.com/video/BV1xx411c7mD/?p=2", "platform": "bilibili",
         "video_id": "BV1xx411c7mD", "page": 2, "cid": "12345", "document_start": 100}
CUE = {"start": 1, "end": 3, "text": "Hello world", "source_language": "en", "subtitle_kind": "human",
       "translation": "你好世界", "target_language": "zh-CN",
       "words": [{"text": "Hello", "start": 1, "end": 2}, {"text": "world", "start": 2, "end": 3}]}


class NativeCaptionPayloadTests(unittest.TestCase):
    def test_keeps_timing_and_bilingual_text_but_no_unrelated_website_fields(self):
        output = normalize_caption_cues([dict(CUE, cookie="private", url="https://secret.test", token="private")])
        self.assertEqual(output[0]["translation_source"], "platform")
        self.assertEqual(output[0]["words"], CUE["words"])
        self.assertEqual(output[0]["timing_source"], "youtube")
        for name in ("cookie", "url", "token"):
            self.assertNotIn(name, output[0])

    def test_invalid_body_is_not_reported_as_no_subtitles(self):
        for body in ([], [{"start": 3, "end": 1, "text": "wrong"}], [dict(CUE, start=float("nan"))],
                     [dict(CUE, text="")], [dict(CUE, words="wrong")]):
            with self.assertRaises((ValueError, TypeError)):
                normalize_caption_cues(body)

    def test_unaligned_words_are_filtered_without_losing_the_sentence(self):
        output = normalize_caption_cues([dict(CUE, words=[{"text": "wrong", "start": 0, "end": 1}])])
        self.assertNotIn("words", output[0])
        self.assertEqual(output[0]["timing_source"], "cue")


class NativeCaptionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = BrowserBridge()
        self.bridge._server = object()  # Dispatch-only test: no TCP listener.
        self.client = "native-caption-test-client"
        self.session = self.bridge._dispatch("/v1/hello", {"client_id": self.client}, "")[1]["session"]
        self.post("/v1/video", {"tab_id": 10, "seq": 1, "video": VIDEO})
        self.video = self.bridge.latest()
        self.events = []
        self.bridge.captions_ready.connect(self.events.append)

    def tearDown(self):
        self.bridge._server = None
        self.bridge.stop()

    def post(self, path, payload):
        return self.bridge._dispatch(path, dict(payload, client_id=self.client), self.session)[1]

    def result(self, request_id, **extra):
        payload = {"id": request_id, "tab_id": 10, "video": VIDEO, "document_start": 100,
                   "status": "ready", "cues": [CUE], "source_language": "en", "caption_source": "youtube-human"}
        return self.post("/v1/captions", dict(payload, **extra))

    def test_async_request_has_exact_tab_identity_and_keeps_translation_poll_compatible(self):
        request = self.bridge.request_captions(self.video, generation=7)
        reply = self.post("/v1/poll", {})
        self.assertEqual(reply["jobs"], [])
        self.assertEqual(len(reply["caption_jobs"]), 1)
        job = reply["caption_jobs"][0]
        self.assertEqual(job["id"], request)
        self.assertEqual(job["tab_id"], 10)
        self.assertEqual(job["document_start"], 100)
        self.assertNotIn("audio_urls", job["video"])
        self.assertEqual(self.post("/v1/poll", {})["caption_jobs"], [])
        self.result(request)
        event = self.events[0]
        self.assertEqual((event["request_id"], event["generation"], event["status"]), (request, 7, "ready"))
        self.assertEqual(event["bridge_key"], self.video["bridge_key"])

    def test_chunked_result_waits_for_all_parts_and_sorts_absolute_seconds(self):
        request = self.bridge.request_captions(self.video, generation=9)
        late = dict(CUE, start=5, end=7, words=[])
        self.assertTrue(self.result(request, part=1, part_count=2, cues=[late])["accepted"])
        self.assertEqual(self.events, [])
        self.assertFalse(self.result(request, part=1, part_count=2, cues=[late])["accepted"])
        self.result(request, part=0, part_count=2)
        self.assertEqual([cue["start"] for cue in self.events[0]["cues"]], [1, 5])
        self.assertNotIn(request, self.bridge._caption_jobs)

    def test_wrong_tab_cid_or_document_does_not_replace_current_video_captions(self):
        request = self.bridge.request_captions(self.video)
        for extra in ({"tab_id": 11}, {"document_start": 101}, {"video": dict(VIDEO, cid="99999")}):
            self.assertFalse(self.result(request, **extra)["accepted"])
        self.assertEqual(self.events, [])
        self.assertTrue(self.result(request)["accepted"])

    def test_missing_track_failed_fetch_and_timeout_are_distinct(self):
        for status in ("unavailable", "error", "timeout"):
            request = self.bridge.request_captions(self.video)
            if status == "timeout":
                self.bridge._caption_timeout(request)
            else:
                self.result(request, status=status, error="provider-secret", cues=[])
            self.assertEqual(self.events[-1]["status"], status)
            self.assertEqual(self.events[-1]["cues"], [])
            self.assertNotIn("provider-secret", json.dumps(self.events[-1]))

    def test_cancel_is_silent_and_late_result_is_ignored(self):
        request = self.bridge.request_captions(self.video)
        self.bridge.cancel_captions(request)
        self.assertFalse(self.result(request)["accepted"])
        self.bridge._caption_timeout(request)
        self.assertEqual(self.events, [])

    def test_same_video_document_reload_and_closed_tab_cancel_old_requests(self):
        request = self.bridge.request_captions(self.video)
        self.post("/v1/video", {"tab_id": 10, "seq": 2, "video": dict(VIDEO, document_start=101)})
        self.assertFalse(self.result(request)["accepted"])
        with self.assertRaises(ValueError):
            self.bridge.request_captions(self.video)
        current = self.bridge.latest()
        next_request = self.bridge.request_captions(current)
        self.post("/v1/closed", {"tab_id": 10, "seq": 3})
        self.assertNotIn(next_request, self.bridge._caption_jobs)
        self.assertEqual(self.events, [])

    def test_malformed_cue_returns_fixed_error_and_finishes_request(self):
        request = self.bridge.request_captions(self.video)
        self.result(request, cues=[dict(CUE, end=float("nan"))])
        self.assertEqual(self.events[0]["status"], "error")
        self.assertNotIn(request, self.bridge._caption_jobs)

    def test_known_existing_track_cannot_be_reported_as_unavailable(self):
        request = self.bridge.request_captions(self.video)
        self.result(request, status="unavailable", cues=[], native_track_available=True,
                    error_code="bili_track_failed")
        self.assertEqual(self.events[0]["status"], "error")
        self.assertTrue(self.events[0]["native_track_available"])
        self.assertEqual(self.events[0]["error_code"], "bili_track_failed")

    def test_fixed_error_enum_is_kept_but_arbitrary_url_or_token_is_not(self):
        for code in ("bili_metadata_failed", "https://secret.test/subtitle?token=private", {"token": "private"}):
            request = self.bridge.request_captions(self.video)
            self.result(request, status="error", cues=[], error_code=code,
                        native_track_available="true", error="private-provider-token")
            event = self.events[-1]
            if code == "bili_metadata_failed":
                self.assertEqual(event["error_code"], code)
            else:
                self.assertNotIn("error_code", event)
            self.assertNotIn("native_track_available", event)
            self.assertNotIn("private", json.dumps(event))

    def test_confirmed_empty_track_remains_unavailable(self):
        request = self.bridge.request_captions(self.video)
        self.result(request, status="unavailable", cues=[], native_track_available=False)
        self.assertEqual(self.events[0]["status"], "unavailable")
        self.assertIs(self.events[0]["native_track_available"], False)


NODE_SETUP = r'''
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const code=fs.readFileSync('assets/desktop-bridge-page.js','utf8');
function setup(href, responder) {
  const handlers=new Map(), messages=[], requests=[];
  const media={currentTime:4,duration:19,paused:false,ended:false,readyState:4,playbackRate:1};
  class Xhr {constructor(){this.handlers=[];} open(){} addEventListener(kind,fn){if(kind==='load')this.handlers.push(fn);} emit(){for(const fn of this.handlers)fn();}}
  const ctx={URL,URLSearchParams,AbortController,Date,setTimeout,clearTimeout,location:new URL(href),
    performance:{timeOrigin:100},XMLHttpRequest:Xhr,setInterval:fn=>{ctx.report=fn;},
    document:{visibilityState:'visible',title:'Example',querySelector:()=>media},
    addEventListener:(kind,fn)=>{const existing=handlers.get(kind)||[];existing.push(fn);handlers.set(kind,existing);},
    postMessage:m=>messages.push(m),fetch:async (url,options)=>{requests.push({url:String(url),options});const result=await responder(String(url),options);return {ok:result.ok!==false,json:async()=>result.payload,clone:()=>({json:async()=>result.payload})};}};
  ctx.window=ctx;ctx.top=ctx;ctx.__handlers=handlers;vm.createContext(ctx);
  return {ctx,media,messages,requests,start:()=>{vm.runInContext(code,ctx);vm.runInContext(`deliver=message=>{for(const fn of __handlers.get('message')||[])fn({source:window,origin:location.origin,data:message});}`,ctx);},
    request:(id,video,preferred='auto')=>ctx.deliver({kind:'trancy-desktop-captions-command-v1',document_start:100,request_id:id,video,preferred_language:preferred})};
}
const requestId='a'.repeat(32);
const wait=async(test)=>{for(let i=0;i<300;i++){if(test())return;await new Promise(resolve=>setTimeout(resolve,10));}throw new Error('test timed out');};
const captionMessage=page=>page.messages.find(m=>m.kind==='trancy-desktop-captions-v1');
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the extension sandbox")
class NativeCaptionPageTests(unittest.TestCase):
    def run_node(self, body, timeout=12):
        result = subprocess.run([shutil.which("node"), "-e", NODE_SETUP + "\n(async()=>{\n" + body +
                                 "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, encoding="utf-8", timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bilibili_native_bilingual_track_and_cache_without_audio(self):
        self.run_node(r'''
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/?p=2',async url=>{
 if(url.includes('/x/player/v2'))return {payload:{code:0,data:{subtitle:{subtitles:[
  {lan:'zh-CN',subtitle_url:'//i0.hdslb.com/bfs/subtitle/zh.json'},
  {lan:'en',subtitle_url:'//i0.hdslb.com/bfs/subtitle/en.json'}]}}}};
 return {payload:{body:[{from:1,to:3,content:url.includes('zh.json')?'你好世界':'Hello world'}]}};
});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',cid:222,title:'Bili',pages:[{page:1,cid:111},{page:2,cid:222}]}};
page.start();const video=page.messages[0].video;page.request(requestId,video);await wait(()=>captionMessage(page));
const message=captionMessage(page);assert.equal(message.status,'ready');assert.equal(message.caption_source,'bilibili-human');
assert.equal(message.cues[0].text,'你好世界');assert.equal(message.cues[0].translation,'Hello world');
assert.equal(message.video.cid,'222');assert.ok(page.requests[0].url.includes('cid=222'));
assert.ok(page.requests.every(r=>!r.url.includes('playurl')&&!r.url.includes('audio')));
assert.equal(page.requests.find(r=>r.url.includes('zh.json')).options.credentials,'omit');
const count=page.requests.length;page.request('b'.repeat(32),video);await wait(()=>page.messages.filter(m=>m.kind==='trancy-desktop-captions-v1').length===2);
assert.equal(page.requests.length,count);
''')

    def test_bilibili_second_language_failure_keeps_original_text(self):
        self.run_node(r'''
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async url=>{
 if(url.includes('/x/player/v2'))return {payload:{code:0,data:{subtitle:{subtitles:[{lan:'ai-zh',subtitle_url:'https://aisubtitle.hdslb.com/bfs/ai_subtitle/zh.json'},
 {lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/en.json'}]}}}};
 if(url.includes('en.json'))return {ok:false};return {payload:{body:[{from:1,to:3,content:'识别出的中文'}]}};
});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',cid:111,pages:[{page:1,cid:111}]}};
page.start();page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'ready');assert.equal(captionMessage(page).caption_source,'bilibili-asr');
assert.equal(captionMessage(page).cues[0].text,'识别出的中文');assert.equal(captionMessage(page).cues[0].translation,undefined);
''')

    def test_bilibili_empty_tracks_and_failed_metadata_are_distinct(self):
        self.run_node(r'''
for(const [payload,status] of [[{code:0,data:{subtitle:{subtitles:[]}}},'unavailable'],[{code:-101,message:'secret login context'},'error']]){
 const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async()=>({payload}));
 page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
 page.start();await page.ctx.fetch('https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111');await new Promise(setImmediate);
 page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
 assert.equal(captionMessage(page).status,status);assert.ok(!JSON.stringify(captionMessage(page)).includes('secret'));
 if(status==='unavailable')assert.equal(captionMessage(page).native_track_available,false);
 else assert.equal(captionMessage(page).error_code,'bili_metadata_failed');
}
''')

    def test_bilibili_aid_wbi_and_selected_cdn_body_are_reused_before_legacy_api(self):
        self.run_node(r'''
for(const selector of ['aid=1','avid=1','bvid=BV1PPQzByEAC']){
 const tracks=[{lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/selected.json'}];let cdn=0;
 const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async url=>{
  if(url.includes('/wbi/v2'))return {payload:{code:0,data:{subtitle:{subtitles:tracks}}}};
  if(url.includes('/bfs/subtitle/'))return (++cdn===1)?{payload:{body:[{from:1,to:3,content:'Already displayed CC'}]}}:{ok:false};
  return {payload:{code:0,data:{subtitle:{subtitles:[]}}}};
 });
 page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
 page.start();await page.ctx.fetch(new URL('https://api.bilibili.com/x/player/wbi/v2?'+selector+'&cid=111'));await new Promise(setImmediate);
 await page.ctx.fetch('https://i0.hdslb.com/bfs/subtitle/selected.json');await new Promise(setImmediate);
 page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
 const message=captionMessage(page);assert.equal(message.status,'ready');assert.equal(message.native_track_available,true);
 assert.equal(message.cues[0].text,'Already displayed CC');assert.equal(cdn,1);
 assert.equal(page.requests.filter(r=>r.url.includes('/x/player/v2')).length,0);
}
''')

    def test_bilibili_native_xhr_metadata_and_subtitle_body_are_reused(self):
        self.run_node(r'''
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async()=>{throw new Error('No fetch is needed');});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
page.start();vm.runInContext(`
const metadata=new XMLHttpRequest();metadata.open('GET','https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111');
metadata.responseURL='https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111';metadata.responseType='json';
metadata.response={code:0,data:{subtitle:{subtitles:[{lan:'ai-zh',subtitle_url:'https://aisubtitle.hdslb.com/bfs/ai_subtitle/chosen.json'}]}}};metadata.emit();
const subtitle=new XMLHttpRequest();subtitle.open('GET','https://aisubtitle.hdslb.com/bfs/ai_subtitle/chosen.json');
subtitle.responseURL='https://aisubtitle.hdslb.com/bfs/ai_subtitle/chosen.json';subtitle.responseText=JSON.stringify({body:[{from:1,to:3,content:'native subtitle'}]});subtitle.emit();
`,page.ctx);
page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'ready');assert.equal(captionMessage(page).caption_source,'bilibili-asr');
assert.equal(page.requests.length,0);
''')

    def test_failed_new_metadata_does_not_erase_successful_track_body_binding(self):
        self.run_node(r'''
let metadata=0;const tracks=[{lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/chosen.json'}];
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async url=>{
 if(url.includes('/wbi/v2'))return ++metadata===1?{payload:{code:0,data:{subtitle:{subtitles:tracks}}}}:{payload:{code:-101}};
 if(url.includes('/bfs/subtitle/'))return {payload:{body:[{from:1,to:3,content:'Previously successful native CC'}]}};
 return {ok:false};
});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
page.start();const native='https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111';
await page.ctx.fetch(native);await new Promise(setImmediate);
await page.ctx.fetch('https://i0.hdslb.com/bfs/subtitle/chosen.json');await new Promise(setImmediate);
await page.ctx.fetch(native);await new Promise(setImmediate);
page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'ready');assert.equal(captionMessage(page).cues[0].text,'Previously successful native CC');
assert.equal(page.requests.filter(r=>r.url.includes('/x/player/v2')).length,0);
assert.equal(page.requests.filter(r=>r.url.includes('/bfs/subtitle/')).length,1);
''')

    def test_late_older_cdn_response_cannot_replace_newer_body(self):
        self.run_node(r'''
let cdn=0,release;const tracks=[{lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/chosen.json'}];
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async url=>{
 if(url.includes('/wbi/v2'))return {payload:{code:0,data:{subtitle:{subtitles:tracks}}}};
 if(url.includes('/bfs/subtitle/')){
  if(++cdn===1)return await new Promise(resolve=>release=resolve);
  return {payload:{body:[{from:1,to:3,content:'Newer subtitle response'}]}};
 }
 return {ok:false};
});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
page.start();await page.ctx.fetch('https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111');await new Promise(setImmediate);
const older=page.ctx.fetch('https://i0.hdslb.com/bfs/subtitle/chosen.json');await wait(()=>Boolean(release));
await page.ctx.fetch('https://i0.hdslb.com/bfs/subtitle/chosen.json');await new Promise(setImmediate);
release({payload:{body:[{from:1,to:3,content:'Older stale subtitle response'}]}});await older;await new Promise(setImmediate);
page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'ready');assert.equal(captionMessage(page).cues[0].text,'Newer subtitle response');assert.equal(cdn,2);
''')

    def test_bilibili_late_native_metadata_can_validate_already_loaded_cdn_body(self):
        self.run_node(r'''
const tracks=[{lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/chosen.json'}];
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async url=>{
 if(url.includes('/wbi/v2'))return {payload:{code:0,data:{subtitle:{subtitles:tracks}}}};
 if(url.includes('/bfs/subtitle/'))return {payload:{body:[{from:1,to:3,content:'late metadata cached body'}]}};
 return {payload:{code:0,data:{subtitle:{subtitles:[]}}}};
});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
page.start();await page.ctx.fetch('https://i0.hdslb.com/bfs/subtitle/chosen.json');await new Promise(setImmediate);
page.request(requestId,page.messages[0].video);
setTimeout(()=>{void page.ctx.fetch('https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111');},180);
await wait(()=>captionMessage(page));assert.equal(captionMessage(page).status,'ready');
assert.equal(captionMessage(page).cues[0].text,'late metadata cached body');
assert.equal(page.requests.filter(r=>r.url.includes('/bfs/subtitle/')).length,1);
''')

    def test_empty_legacy_api_without_native_confirmation_does_not_claim_no_subtitles(self):
        self.run_node(r'''
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async()=>({payload:{code:0,data:{subtitle:{subtitles:[]}}}}));
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
page.start();page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'error');assert.equal(captionMessage(page).error_code,'bili_metadata_failed');
assert.equal(captionMessage(page).native_track_available,undefined);
''')

    def test_bilibili_existing_track_download_failure_is_known_available(self):
        self.run_node(r'''
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async url=>url.includes('/wbi/v2')?
 {payload:{code:0,data:{subtitle:{subtitles:[{lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/missing.json'}]}}}}:{ok:false});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
page.start();await page.ctx.fetch('https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111');await new Promise(setImmediate);
page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'error');assert.equal(captionMessage(page).native_track_available,true);
assert.equal(captionMessage(page).error_code,'bili_track_failed');
''')

    def test_bilibili_other_cid_body_cannot_be_reused_after_part_switch(self):
        self.run_node(r'''
const tracks=[{lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/shared.json'}];let cdn=0;
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/?p=1',async url=>{
 if(url.includes('/wbi/v2'))return {payload:{code:0,data:{subtitle:{subtitles:tracks}}}};
 if(url.includes('/bfs/subtitle/'))return {payload:{body:[{from:1,to:3,content:++cdn===1?'Part one':'Part two'}]}};
 return {payload:{code:0,data:{subtitle:{subtitles:[]}}}};
});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111},{page:2,cid:222}]}};
page.start();await page.ctx.fetch('https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111');await new Promise(setImmediate);
await page.ctx.fetch('https://i0.hdslb.com/bfs/subtitle/shared.json');await new Promise(setImmediate);
page.ctx.location=new URL('https://www.bilibili.com/video/BV1PPQzByEAC/?p=2');page.ctx.report();
await page.ctx.fetch('https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=222');await new Promise(setImmediate);
page.request(requestId,page.messages.at(-1).video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'ready');assert.equal(captionMessage(page).cues[0].text,'Part two');assert.equal(cdn,2);
''')

    def test_bilibili_prefetched_unlisted_cdn_does_not_become_current_subtitle(self):
        self.run_node(r'''
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async url=>{
 if(url.includes('/wbi/v2'))return {payload:{code:0,data:{subtitle:{subtitles:[{lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/current.json'}]}}}};
 return {payload:{body:[{from:1,to:3,content:url.includes('foreign.json')?'Foreign preloaded video':'Current correct video'}]}};
});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
page.start();await page.ctx.fetch('https://api.bilibili.com/x/player/wbi/v2?aid=1&cid=111');await new Promise(setImmediate);
await page.ctx.fetch('https://i0.hdslb.com/bfs/subtitle/foreign.json');await new Promise(setImmediate);
page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).cues[0].text,'Current correct video');
''')

    def test_bilibili_mismatched_aid_metadata_is_not_bound_to_current_bv(self):
        self.run_node(r'''
const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/',async url=>url.includes('/wbi/v2')?
 {payload:{code:0,data:{subtitle:{subtitles:[{lan:'en',subtitle_url:'https://i0.hdslb.com/bfs/subtitle/wrong.json'}]}}}}:
 {payload:{code:0,data:{subtitle:{subtitles:[]}}}});
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',aid:1,cid:111,pages:[{page:1,cid:111}]}};
page.start();await page.ctx.fetch('https://api.bilibili.com/x/player/wbi/v2?bvid=BV1PPQzByEAC&aid=2&cid=111');await new Promise(setImmediate);
page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'error');assert.equal(captionMessage(page).error_code,'bili_metadata_failed');
assert.equal(page.requests.filter(r=>r.url.includes('/bfs/subtitle/')).length,0);
''')

    def test_bilibili_navigation_discards_old_cid_fetch(self):
        self.run_node(r'''
let release;const page=setup('https://www.bilibili.com/video/BV1PPQzByEAC/?p=1',()=>new Promise(resolve=>release=resolve));
page.ctx.__INITIAL_STATE__={videoData:{bvid:'BV1PPQzByEAC',cid:111,pages:[{page:1,cid:111},{page:2,cid:222}]}};
page.start();page.request(requestId,page.messages[0].video);await wait(()=>Boolean(release));
page.ctx.location=new URL('https://www.bilibili.com/video/BV1PPQzByEAC/?p=2');page.ctx.report();
release({payload:{code:0,data:{subtitle:{subtitles:[]}}}});await new Promise(resolve=>setTimeout(resolve,20));
assert.equal(captionMessage(page),undefined);assert.equal(page.messages.at(-1).video.cid,'222');
''')

    def test_youtube_human_track_preferred_and_real_words_preserved_for_asr(self):
        self.run_node(r'''
for(const human of [true,false]){
 const base='https://www.youtube.com/api/timedtext?v=jNQXAC9IVRw&lang=en';
 const page=setup('https://www.youtube.com/watch?v=jNQXAC9IVRw',async url=>({payload:{events:[{tStartMs:1000,dDurationMs:2000,
  segs:[{utf8:'Hello ',tOffsetMs:0},{utf8:'world',tOffsetMs:1000}]}]}}));
 const tracks=[{languageCode:'en',kind:'asr',baseUrl:base+'&kind=asr'},...(human?[{languageCode:'en',baseUrl:base}]:[])];
 const player={getPlayerResponse:()=>({videoDetails:{videoId:'jNQXAC9IVRw',title:'YT'},captions:{playerCaptionsTracklistRenderer:{captionTracks:tracks}}}),
  querySelector:()=>page.media,classList:{contains:()=>false}};
 page.ctx.document.querySelector=()=>player;page.start();page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
 const message=captionMessage(page);assert.equal(message.status,'ready');assert.equal(message.caption_source,human?'youtube-human':'youtube-asr');
 assert.equal(message.cues[0].text,'Hello world');assert.equal(message.cues[0].words[1].start,2);assert.equal(message.cues[0].words[1].end,3);
 assert.equal(new URL(page.requests[0].url).searchParams.get('kind'),human?null:'asr');
 assert.equal(new URL(page.requests[0].url).searchParams.get('fmt'),'json3');
}
''')

    def test_youtube_native_request_context_fallback_keeps_signed_url_in_page(self):
        self.run_node(r'''
const base='https://www.youtube.com/api/timedtext?v=jNQXAC9IVRw&lang=en&kind=asr';
const page=setup('https://www.youtube.com/watch?v=jNQXAC9IVRw',async()=>({ok:false}));
const payload={events:[{tStartMs:1000,dDurationMs:2000,segs:[{utf8:'Hello ',tOffsetMs:0},{utf8:'world',tOffsetMs:1000}]}]};
page.ctx.__payload=payload;vm.runInContext(`nativeLoaded=()=>{const xhr=new XMLHttpRequest();xhr.open('GET','${base}&fmt=json3&pot=private-proof');
 xhr.responseURL='${base}&fmt=json3&pot=private-proof';xhr.responseType='json';xhr.response=__payload;xhr.emit();}`,page.ctx);
const player={getPlayerResponse:()=>({videoDetails:{videoId:'jNQXAC9IVRw'},captions:{playerCaptionsTracklistRenderer:{captionTracks:[{languageCode:'en',kind:'asr',baseUrl:base}]}}}),
 querySelector:()=>page.media,classList:{contains:()=>false},loadModule:()=>{},setOption:()=>page.ctx.nativeLoaded()};
page.ctx.document.querySelector=()=>player;page.start();page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'ready');assert.equal(captionMessage(page).cues[0].text,'Hello world');
assert.ok(!JSON.stringify(captionMessage(page)).includes('private-proof'));assert.ok(!JSON.stringify(captionMessage(page)).includes('baseUrl'));
''')

    def test_unrelated_caption_host_is_never_requested(self):
        self.run_node(r'''
const page=setup('https://www.youtube.com/watch?v=jNQXAC9IVRw',async()=>{throw new Error('must not fetch');});
const player={getPlayerResponse:()=>({videoDetails:{videoId:'jNQXAC9IVRw'},captions:{playerCaptionsTracklistRenderer:{captionTracks:[
 {languageCode:'en',baseUrl:'https://evil.test/api/timedtext?v=jNQXAC9IVRw&lang=en'}]}}}),querySelector:()=>page.media,classList:{contains:()=>false}};
page.ctx.document.querySelector=()=>player;page.start();page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'error');assert.equal(page.requests.length,0);
assert.equal(captionMessage(page).error_code,'youtube_track_failed');assert.equal(captionMessage(page).native_track_available,true);
''')

    def test_youtube_confirmed_empty_tracks_mark_availability_false(self):
        self.run_node(r'''
const page=setup('https://www.youtube.com/watch?v=jNQXAC9IVRw',async()=>{throw new Error('must not fetch');});
const player={getPlayerResponse:()=>({videoDetails:{videoId:'jNQXAC9IVRw'},captions:{playerCaptionsTracklistRenderer:{captionTracks:[]}}}),
 querySelector:()=>page.media,classList:{contains:()=>false}};
page.ctx.document.querySelector=()=>player;page.start();page.request(requestId,page.messages[0].video);await wait(()=>captionMessage(page));
assert.equal(captionMessage(page).status,'unavailable');assert.equal(captionMessage(page).native_track_available,false);assert.equal(page.requests.length,0);
''')

    def test_isolated_caption_relay_keeps_request_origin_and_diagnostic_fields(self):
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let listener;const messages=[],commands=[];const handlers=new Map();
const ctx={location:new URL('https://www.bilibili.com/video/BV1PPQzByEAC/'),performance:{timeOrigin:100},setTimeout:()=>1,
 chrome:{runtime:{id:'relay-test',onMessage:{addListener:fn=>listener=fn},sendMessage:async message=>messages.push(message)}},
 addEventListener:(name,fn)=>handlers.set(name,fn),postMessage:message=>commands.push(message)};
ctx.window=ctx;ctx.top=ctx;ctx.__handlers=handlers;vm.createContext(ctx);
vm.runInContext(fs.readFileSync('assets/desktop-bridge-content.js','utf8'),ctx);
vm.runInContext(`deliver=(data,origin=location.origin)=>__handlers.get('message')({source:window,origin,data});`,ctx);
const video={platform:'bilibili',video_id:'BV1PPQzByEAC',page:1,cid:'111',url:ctx.location.href};
const id='a'.repeat(32),command={kind:'trancy-desktop-captions-request-v1',document_start:100,request_id:id,video};
let accepted;listener(command,{id:'foreign-extension'},x=>accepted=x);assert.equal(accepted,undefined);assert.equal(commands.length,0);
listener(command,{id:'relay-test'},x=>accepted=x);assert.equal(accepted.accepted,true);assert.equal(commands.length,1);
const result={kind:'trancy-desktop-captions-v1',document_start:100,sequence:1,href:ctx.location.href,request_id:id,video,status:'error',
 error_code:'bili_track_failed',native_track_available:true,cues:[],token:'private-secret',url:'https://secret.test'};
ctx.deliver(result,'https://foreign.test');assert.equal(messages.length,0);
ctx.deliver(result);assert.equal(messages.length,1);assert.equal(messages[0].error_code,'bili_track_failed');assert.equal(messages[0].native_track_available,true);
assert.ok(!JSON.stringify(messages).includes('private-secret'));assert.ok(!JSON.stringify(messages).includes('secret.test'));
'''
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_background_diagnostic_whitelist_retains_only_enum_and_boolean(self):
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{
 let listener,timer;const jobs=[],requests=[];
 const ctx={URL,AbortSignal,crypto:require('node:crypto').webcrypto,setTimeout:fn=>{timer=fn;return 1;},clearTimeout:()=>{},
 chrome:{runtime:{id:'diagnostic-test',getManifest:()=>({name:'Trancy 桌面字幕连接器',version:'0.1.3'}),getPlatformInfo:async()=>({}),onMessage:{addListener:fn=>listener=fn}},
 tabs:{onRemoved:{addListener:()=>{}},onUpdated:{addListener:()=>{}},sendMessage:async()=>({accepted:true})},alarms:{create:()=>{},onAlarm:{addListener:()=>{}}}},
 fetch:async(url,options)=>{const body=JSON.parse(options.body);requests.push({url,body});return {ok:true,json:async()=>url.endsWith('/hello')?{session:'test'}:url.endsWith('/poll')?{jobs:[],caption_jobs:jobs.splice(0)}:{accepted:true}};}};
 ctx.self=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync('assets/desktop-bridge-background.js','utf8'),ctx);ctx.TrancyDesktopBridge.start({getState:async()=>({})});await new Promise(setImmediate);
 const video={platform:'bilibili',video_id:'BV1PPQzByEAC',page:1,cid:'111',url:'https://www.bilibili.com/video/BV1PPQzByEAC/',position:5,duration:50,audio_urls:[]};
 const sender={id:'diagnostic-test',frameId:0,tab:{id:10,url:video.url,active:true}};
 listener({kind:'trancy-desktop-report-v1',href:video.url,document_start:100,sequence:1,video},sender);await timer();
 for(const [index,error_code,available] of [[1,'bili_track_failed',true],[2,'https://secret.test/?token=private-secret','true']]){
  const id=String(index).repeat(32);jobs.push({id,tab_id:10,video,document_start:100});await timer();await new Promise(setImmediate);
  listener({kind:'trancy-desktop-captions-report-v1',href:video.url,document_start:100,sequence:index+1,request_id:id,video,status:'error',error_code,native_track_available:available,cues:[],token:'private-secret'},sender);
  await new Promise(setImmediate);
 }
 const output=requests.filter(r=>r.url.endsWith('/captions'));assert.equal(output.length,2);
 assert.equal(output[0].body.error_code,'bili_track_failed');assert.equal(output[0].body.native_track_available,true);
 assert.equal(output[1].body.error_code,undefined);assert.equal(output[1].body.native_track_available,undefined);
 assert.ok(!JSON.stringify(output).includes('private-secret'));assert.ok(!JSON.stringify(output).includes('secret.test'));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_background_forwards_only_matching_tab_and_chunks_large_track(self):
        script = r'''
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
(async()=>{
 let listener,timer;const jobs=[],requests=[],commands=[];
 const ctx={URL,AbortSignal,crypto:require('node:crypto').webcrypto,setTimeout:fn=>{timer=fn;return 1;},clearTimeout:()=>{},
 chrome:{runtime:{id:'native-test',getManifest:()=>({name:'Trancy 桌面字幕连接器'}),getPlatformInfo:async()=>({}),onMessage:{addListener:fn=>listener=fn}},
 tabs:{onRemoved:{addListener:()=>{}},onUpdated:{addListener:()=>{}},sendMessage:async (id,message,options)=>{commands.push({id,message,options});return {accepted:true};}},
 alarms:{create:()=>{},onAlarm:{addListener:()=>{}}}},fetch:async(url,options)=>{const body=JSON.parse(options.body);requests.push({url,body});
 return {ok:true,json:async()=>url.endsWith('/hello')?{session:'test'}:url.endsWith('/poll')?{jobs:[],caption_jobs:jobs.splice(0)}:{accepted:true}};}};
 ctx.self=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync('assets/desktop-bridge-background.js','utf8'),ctx);
 ctx.TrancyDesktopBridge.start({getState:async()=>({})});await new Promise(setImmediate);
 const video={platform:'youtube',video_id:'jNQXAC9IVRw',url:'https://www.youtube.com/watch?v=jNQXAC9IVRw',page:1,cid:'',position:5,duration:50,audio_urls:[]};
 const sender=id=>({id:'native-test',frameId:0,tab:{id,url:video.url,active:true}});
 listener({kind:'trancy-desktop-report-v1',href:video.url,document_start:100,sequence:1,video},sender(10));await timer();
 const id='a'.repeat(32);jobs.push({id,tab_id:10,key:'youtube:jNQXAC9IVRw',video,document_start:100});await timer();await new Promise(setImmediate);
 assert.equal(commands.length,1);assert.equal(commands[0].options.frameId,0);
 const cues=Array.from({length:161},(_,i)=>({start:i,end:i+1,text:'Caption '+i,source_language:'en',cookie:'private-secret',url:'https://secret.test'}));
 const message={kind:'trancy-desktop-captions-report-v1',href:video.url,document_start:100,sequence:2,request_id:id,video,status:'ready',source_language:'en',caption_source:'youtube-human',cues};
 listener(message,sender(11));await new Promise(setImmediate);assert.equal(requests.filter(r=>r.url.endsWith('/captions')).length,0);
 listener({...message,document_start:99},sender(10));await new Promise(setImmediate);assert.equal(requests.filter(r=>r.url.endsWith('/captions')).length,0);
 listener(message,sender(10));await new Promise(setImmediate);await new Promise(setImmediate);
 const results=requests.filter(r=>r.url.endsWith('/captions'));assert.equal(results.length,2);assert.equal(results[0].body.part_count,2);
 assert.equal(results[0].body.cues.length,160);assert.equal(results[1].body.cues.length,1);
 assert.ok(!JSON.stringify(results).includes('private-secret'));assert.ok(!JSON.stringify(results).includes('secret.test'));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
