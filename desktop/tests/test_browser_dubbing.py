"""Bilibili mute leases and playback events, without launching Chrome or TTS."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trancy_desktop.browser_bridge import BrowserBridge, normalize_video


VIDEO = {"url": "https://www.bilibili.com/video/BV1xx411c7mD/?p=2", "platform": "bilibili",
         "video_id": "BV1xx411c7mD", "page": 2, "cid": "12345", "document_start": 100,
         "dubbing_protocol": 1, "position": 7, "duration": 120, "paused": False}
LEASE = "a" * 32


class DubbingBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = BrowserBridge()
        self.bridge._server = object()  # Dispatch-only: no real HTTP listener.
        self.client = "bilibili-dubbing-test-client"
        self.session = self.bridge._dispatch("/v1/hello", {
            "client_id": self.client, "connector_version": "0.1.5"}, "")[1]["session"]
        self.post("/v1/video", {"tab_id": 10, "seq": 1, "video": VIDEO})
        self.video = self.bridge.latest()
        self.events = []
        self.bridge.dubbing_changed.connect(self.events.append)

    def tearDown(self):
        self.bridge._server = None
        self.bridge.stop()

    def post(self, path, payload):
        return self.bridge._dispatch(path, dict(payload, client_id=self.client), self.session)[1]

    def result(self, request_id, **extra):
        payload = {"id": request_id, "lease_id": LEASE, "tab_id": 10, "video": VIDEO,
                   "document_start": 100, "status": "active", "position": 8, "paused": False,
                   "rate": 1.5, "seeking": False, "buffering": False, "seek_serial": 2}
        return self.post("/v1/dubbing", dict(payload, **extra))

    def test_default_is_off_and_only_explicit_request_reaches_selected_tab(self):
        self.assertEqual(self.post("/v1/poll", {})["dubbing_jobs"], [])
        request = self.bridge.set_dubbing(self.video, True, LEASE)
        jobs = self.post("/v1/poll", {})["dubbing_jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]["id"], jobs[0]["tab_id"], jobs[0]["enabled"]), (request, 10, True))
        self.assertEqual(jobs[0]["video"]["cid"], "12345")
        self.assertNotIn("audio_urls", jobs[0]["video"])
        self.assertEqual(self.post("/v1/poll", {})["dubbing_jobs"], [])
        self.assertTrue(self.result(request)["accepted"])
        event = self.events[0]
        self.assertEqual((event["lease_id"], event["status"], event["seek_serial"]), (LEASE, "active", 2))
        self.assertEqual((event["position"], event["rate"], event["bridge_key"]), (8, 1.5, self.video["bridge_key"]))

    def test_previous_renewal_ack_cannot_reenable_a_disabled_lease(self):
        first = self.bridge.set_dubbing(self.video, True, LEASE)
        renewed = self.bridge.set_dubbing(self.video, True, LEASE)
        self.assertFalse(self.result(first)["accepted"])
        self.assertTrue(self.result(renewed)["accepted"])
        disabled = self.bridge.set_dubbing(self.video, False, LEASE)
        self.assertFalse(self.result(renewed)["accepted"])
        self.assertTrue(self.result(disabled, status="restored", reason="disabled")["accepted"])
        self.assertFalse(self.events[-1]["enabled"])
        self.assertEqual(self.bridge._dubbing_leases, {})

    def test_spontaneous_user_override_after_ack_finishes_lease_and_rejects_late_ack(self):
        request = self.bridge.set_dubbing(self.video, True, LEASE)
        self.result(request)
        self.assertTrue(self.result(request, status="user_override", reason="user_volume")["accepted"])
        self.assertFalse(self.result(request)["accepted"])
        self.assertEqual([event["status"] for event in self.events], ["active", "user_override"])

    def test_identity_is_bound_to_cid_tab_document_client_and_known_request(self):
        request = self.bridge.set_dubbing(self.video, True, LEASE)
        for wrong in ({"tab_id": 11}, {"document_start": 101}, {"video": dict(VIDEO, cid="99999")},
                      {"id": "f" * 32}, {"lease_id": "b" * 32}):
            self.assertFalse(self.result(request, **wrong)["accepted"])
        other_client = "another-dubbing-test-client"
        session = self.bridge._dispatch("/v1/hello", {"client_id": other_client}, "")[1]["session"]
        response = self.bridge._dispatch("/v1/dubbing", {
            "client_id": other_client, "id": request, "lease_id": LEASE, "tab_id": 10,
            "video": VIDEO, "document_start": 100, "status": "active"}, session)[1]
        self.assertFalse(response["accepted"])
        self.assertEqual(self.events, [])
        self.assertTrue(self.result(request)["accepted"])

    def test_old_page_without_protocol_cannot_silently_mute(self):
        self.post("/v1/video", {"tab_id": 10, "seq": 2, "video": dict(VIDEO, dubbing_protocol=0)})
        with self.assertRaisesRegex(ValueError, "重新加载"):
            self.bridge.set_dubbing(self.bridge.latest(), True, LEASE)
        self.assertEqual(self.post("/v1/poll", {})["dubbing_jobs"], [])

    def test_first_phase_rejects_youtube_missing_cid_ads_live_and_invalid_tokens(self):
        for wrong in (dict(self.video, cid=""), dict(self.video, tab_id=11), dict(self.video, source_id="other")):
            with self.assertRaises(ValueError):
                self.bridge.set_dubbing(wrong, True, LEASE)
        youtube = {"url": "https://www.youtube.com/watch?v=abcdefghijk", "platform": "youtube", "video_id": "abcdefghijk"}
        with self.assertRaises(ValueError):
            self.bridge.set_dubbing(youtube, True, LEASE)
        for token in ("private", 3, "f" * 31):
            with self.assertRaises(ValueError):
                self.bridge.set_dubbing(self.video, True, token)
        with self.assertRaises(ValueError):
            self.bridge.set_dubbing(self.video, 1, LEASE)
        for seq, field in enumerate(("ad_playing", "is_live"), 2):
            self.post("/v1/video", {"tab_id": 10, "seq": seq, "video": dict(VIDEO, **{field: True})})
            with self.assertRaises(ValueError):
                self.bridge.set_dubbing(self.bridge.latest(), True, LEASE)

    def test_switching_selected_tab_queues_old_restore_before_new_enable(self):
        self.bridge.set_dubbing(self.video, True, LEASE)
        self.post("/v1/poll", {})
        self.post("/v1/video", {"tab_id": 11, "seq": 1, "video": VIDEO})
        second = next(v for v in self.bridge.videos() if v["tab_id"] == 11)
        request = self.bridge.set_dubbing(second, True, "b" * 32)
        jobs = self.post("/v1/poll", {})["dubbing_jobs"]
        self.assertEqual([(j["tab_id"], j["enabled"]) for j in jobs], [(10, False), (11, True)])
        self.assertEqual(jobs[-1]["id"], request)

    def test_reload_closed_tab_and_stale_connection_cancel_pending_and_notify_desktop(self):
        self.bridge.set_dubbing(self.video, True, LEASE)
        self.post("/v1/video", {"tab_id": 10, "seq": 2, "video": dict(VIDEO, document_start=101)})
        self.assertEqual(self.events[-1]["reason"], "video_changed")
        self.bridge.set_dubbing(self.bridge.latest(), True, "b" * 32)
        self.post("/v1/closed", {"tab_id": 10, "seq": 3})
        self.assertEqual(self.events[-1]["reason"], "page_closed")
        self.post("/v1/video", {"tab_id": 10, "seq": 4, "video": VIDEO})
        self.bridge.set_dubbing(self.bridge.latest(), True, "c" * 32)
        self.bridge._videos[self.video["bridge_key"]]["_seen"] = time.monotonic() - 9
        self.assertEqual(self.bridge.videos(), [])
        self.assertEqual(self.events[-1]["reason"], "bridge_disconnected")
        self.assertEqual(self.bridge._dubbing_jobs, {})

    def test_timeout_remains_releasable_and_only_fixed_reason_crosses_bridge(self):
        request = self.bridge.set_dubbing(self.video, True, LEASE)
        self.bridge._dubbing_timeout(request)
        self.assertEqual(self.events[-1]["status"], "timeout")
        release = self.bridge.set_dubbing(self.video, False, LEASE)
        self.result(release, status="restored", reason="https://private.test/?token=secret", cookie="secret")
        self.assertEqual(self.events[-1]["reason"], "")
        self.assertNotIn("secret", json.dumps(self.events))

    def test_playback_fields_are_bounded_and_protocol_requires_integer_one(self):
        normal = normalize_video(dict(VIDEO, seeking=True, buffering=True, seek_serial=4, playback_event="seeking"))
        self.assertEqual((normal["seeking"], normal["buffering"], normal["seek_serial"], normal["playback_event"]), (True, True, 4, "seeking"))
        normal = normalize_video(dict(VIDEO, dubbing_protocol=True, playback_event={"token": "private"}))
        self.assertEqual((normal["dubbing_protocol"], normal["playback_event"]), (0, ""))
        request = self.bridge.set_dubbing(self.video, True, LEASE)
        self.result(request, position=float("nan"), rate=float("inf"), seek_serial=-2)
        self.assertEqual((self.events[-1]["position"], self.events[-1]["rate"], self.events[-1]["seek_serial"]), (0, 1, 0))

    def test_natural_end_survives_periodic_reports_and_manual_pause_is_distinct(self):
        for sequence, ended, playback_event in ((2, True, "ended"), (3, True, ""), (4, False, "pause")):
            self.post("/v1/video", {"tab_id": 10, "seq": sequence,
                "video": dict(VIDEO, position=120, paused=True, ended=ended, playback_event=playback_event)})
            current = self.bridge.latest()
            self.assertTrue(current["paused"])
            self.assertIs(current["ended"], ended)
            self.assertEqual(current["playback_event"], playback_event)
        # Being near the duration, or having only an event string, does not
        # turn an old connector's manual pause into a natural end.
        self.assertFalse(normalize_video(dict(VIDEO, position=120, paused=True, playback_event="ended"))["ended"])
        self.assertFalse(normalize_video(dict(VIDEO, ended="true"))["ended"])

    def test_dubbing_ack_keeps_ended_independent_of_paused_and_rejects_non_boolean_values(self):
        for ended in (True, False, None, "true", 1):
            request = self.bridge.set_dubbing(self.video, True, LEASE)
            fields = {} if ended is None else {"ended": ended}
            self.assertTrue(self.result(request, paused=True, position=120, **fields)["accepted"])
            event = self.events[-1]
            self.assertTrue(event["paused"])
            self.assertIs(event["ended"], ended is True)


PAGE_SETUP = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const pageCode=fs.readFileSync('assets/desktop-bridge-page.js','utf8');
function makeMedia(muted=false,volume=.8){
 const listeners=new Map();let currentMuted=muted,currentVolume=volume;
 const media={currentTime:5,duration:120,paused:false,ended:false,readyState:4,playbackRate:1,seeking:false,
  addEventListener:(name,fn)=>{const list=listeners.get(name)||[];list.push(fn);listeners.set(name,list);},
  removeEventListener:(name,fn)=>listeners.set(name,(listeners.get(name)||[]).filter(f=>f!==fn)),
  emit:name=>{for(const fn of [...listeners.get(name)||[]])fn();}};
 Object.defineProperties(media,{muted:{get:()=>currentMuted,set:v=>{const changed=currentMuted!==v;currentMuted=v;if(changed)media.emit('volumechange');}},
  volume:{get:()=>currentVolume,set:v=>{const changed=currentVolume!==v;currentVolume=v;if(changed)media.emit('volumechange');}}});
 return media;
}
function setup(muted=false){
 const handlers=new Map(),messages=[],timers=new Map();let now=1000,serial=0;
 class Clock extends Date{static now(){return now;}}
 class Xhr{open(){}addEventListener(){}}
 const page={media:makeMedia(muted),ad:false};
 const ctx={URL,URLSearchParams,AbortController,Date:Clock,performance:{timeOrigin:100},XMLHttpRequest:Xhr,
  location:new URL('https://www.bilibili.com/video/BV1xx411c7mD/?p=2'),
  document:{visibilityState:'visible',title:'Example',querySelector:s=>s.includes('ad-tip')?(page.ad?{offsetParent:{}}:null):page.media},
  setTimeout:(fn,delay)=>{const id=++serial;timers.set(id,{fn,due:now+delay});return id;},clearTimeout:id=>timers.delete(id),
  setInterval:fn=>{page.report=fn;},fetch:async()=>{throw Error('unexpected network request');},
  addEventListener:(name,fn)=>{const list=handlers.get(name)||[];list.push(fn);handlers.set(name,list);},postMessage:m=>messages.push(m)};
 ctx.window=ctx;ctx.top=ctx;ctx.__INITIAL_STATE__={videoData:{bvid:'BV1xx411c7mD',cid:12345,pages:[{page:2,cid:12345}],title:'Example'}};
 ctx.__handlers=handlers;vm.createContext(ctx);vm.runInContext(pageCode,ctx);
 vm.runInContext(`deliver=message=>{for(const fn of __handlers.get('message')||[])fn({source:window,origin:location.origin,data:message});}`,ctx);
 page.ctx=ctx;page.messages=messages;page.video={platform:'bilibili',video_id:'BV1xx411c7mD',url:ctx.location.href,page:2,cid:'12345'};
 page.command=(enabled,request='a'.repeat(32),lease='b'.repeat(32),extra={})=>ctx.deliver({kind:'trancy-desktop-dubbing-command-v1',
  document_start:100,request_id:request,lease_id:lease,enabled,video:page.video,...extra});
 page.advance=ms=>{now+=ms;for(const [id,timer]of [...timers])if(timer.due<=now){timers.delete(id);timer.fn();}};
 page.emit=name=>{for(const fn of handlers.get(name)||[])fn();};
 page.results=()=>messages.filter(m=>m.kind==='trancy-desktop-dubbing-v1');
 page.videos=()=>messages.filter(m=>m.kind==='trancy-desktop-video-v1');
 return page;
}
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the extension sandbox")
class DubbingPageTests(unittest.TestCase):
    def run_node(self, body):
        result = subprocess.run([shutil.which("node"), "-e", PAGE_SETUP + "\n" + body],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_off_enable_renew_disable_preserves_original_mute_and_volume(self):
        self.run_node(r'''
for(const originallyMuted of [false,true]){
 const page=setup(originallyMuted);assert.equal(page.media.muted,originallyMuted);assert.equal(page.results().length,0);
 assert.equal(page.videos()[0].video.dubbing_protocol,1);
 page.command(true);assert.equal(page.media.muted,true);assert.equal(page.results().at(-1).status,'active');
 page.advance(2000);page.command(true,'c'.repeat(32));page.advance(2000);assert.equal(page.media.muted,true);
 page.command(false,'d'.repeat(32));assert.equal(page.media.muted,originallyMuted);assert.equal(page.media.volume,.8);
 assert.equal(page.results().at(-1).status,'restored');page.advance(4000);assert.equal(page.results().at(-1).status,'restored');
}
''')

    def test_disconnect_expiry_restores_and_delayed_renewal_cannot_remute(self):
        self.run_node(r'''
const page=setup();page.command(true);page.advance(3000);
assert.equal(page.media.muted,false);assert.equal(page.results().at(-1).status,'expired');
page.command(true,'c'.repeat(32));assert.equal(page.media.muted,false);assert.equal(page.results().at(-1).reason,'retired_lease');
page.command(true,'d'.repeat(32),'e'.repeat(32));assert.equal(page.media.muted,true);
page.command(false,'f'.repeat(32),'e'.repeat(32),{reason:'bridge_disconnected'});
assert.equal(page.media.muted,false);assert.equal(page.results().at(-1).reason,'bridge_disconnected');
''')

    def test_user_volume_and_unmute_override_relinquishes_without_clobbering_intent(self):
        self.run_node(r'''
for(const original of [false,true]){
 const page=setup(original);page.command(true);page.media.volume=.35;
 assert.equal(page.media.volume,.35);assert.equal(page.media.muted,original);
 assert.equal(page.results().at(-1).status,'user_override');page.command(true,'c'.repeat(32));assert.equal(page.media.muted,original);
}
const page=setup();page.command(true);page.media.muted=false;
assert.equal(page.results().at(-1).status,'user_override');assert.equal(page.media.muted,false);
page.command(false,'d'.repeat(32));assert.equal(page.media.muted,false);
''')

    def test_wrong_cid_document_youtube_and_replaced_element_never_mute_new_player(self):
        self.run_node(r'''
const page=setup();page.command(true,undefined,undefined,{video:{...page.video,cid:'99999'}});
assert.equal(page.media.muted,false);assert.equal(page.results().at(-1).reason,'video_changed');
const count=page.results().length;page.command(true,undefined,undefined,{document_start:101});
page.command(true,undefined,undefined,{video:{...page.video,platform:'youtube'}});assert.equal(page.results().length,count);
page.command(true);const original=page.media;page.media=makeMedia();page.report();
assert.equal(original.muted,false);assert.equal(page.media.muted,false);assert.equal(page.results().at(-1).status,'video_changed');
''')

    def test_pause_seek_buffer_and_rate_events_report_immediately_without_timer(self):
        self.run_node(r'''
const page=setup();page.media.paused=true;page.media.emit('pause');assert.equal(page.videos().at(-1).video.paused,true);
page.media.currentTime=67;page.media.seeking=true;page.media.emit('seeking');
let report=page.videos().at(-1).video;assert.equal(report.position,67);assert.equal(report.seeking,true);assert.equal(report.seek_serial,1);
page.media.seeking=false;page.media.emit('seeked');assert.equal(page.videos().at(-1).video.seek_serial,1);
page.media.readyState=2;page.media.emit('waiting');assert.equal(page.videos().at(-1).video.buffering,true);
page.media.paused=false;page.media.readyState=4;page.media.emit('playing');assert.equal(page.videos().at(-1).video.buffering,false);
page.media.playbackRate=1.5;page.media.emit('ratechange');assert.equal(page.videos().at(-1).video.rate,1.5);
''')

    def test_natural_end_is_durable_in_reports_and_acks_but_manual_pause_is_not_end(self):
        self.run_node(r'''
const page=setup();page.media.currentTime=120;page.media.paused=true;page.media.emit('pause');
let report=page.videos().at(-1).video;assert.equal(report.paused,true);assert.equal(report.ended,false);
page.command(true);assert.equal(page.results().at(-1).ended,false);assert.equal(page.results().at(-1).paused,true);
page.media.ended=true;page.media.emit('ended');report=page.videos().at(-1).video;
assert.equal(report.ended,true);assert.equal(report.playback_event,'ended');
page.report();report=page.videos().at(-1).video;assert.equal(report.ended,true);assert.equal(report.playback_event,'');
page.command(true,'c'.repeat(32));assert.equal(page.results().at(-1).ended,true);assert.equal(page.results().at(-1).paused,true);
page.media.currentTime=0;page.media.ended=false;page.media.seeking=true;page.media.emit('seeking');
report=page.videos().at(-1).video;assert.equal(report.ended,false);assert.equal(report.seeking,true);
page.command(true,'d'.repeat(32));assert.equal(page.results().at(-1).ended,false);assert.equal(page.results().at(-1).seeking,true);
''')

    def test_ad_and_pagehide_restore_without_starting_or_seeking_video(self):
        self.run_node(r'''
const page=setup();page.command(true);page.ad=true;page.report();assert.equal(page.media.muted,false);
assert.equal(page.results().at(-1).reason,'ad_playing');assert.equal(page.videos().at(-1).video.ad_playing,true);
page.ad=false;page.command(true,'c'.repeat(32),'d'.repeat(32));page.emit('pagehide');
assert.equal(page.media.muted,false);assert.equal(page.results().at(-1).reason,'page_closed');
assert.equal(page.media.currentTime,5);assert.equal(page.media.paused,false);
''')


BACKGROUND_SETUP = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
function setup(){
 let listener,timer,fail=false;const posts=[],commands=[],queue=[];
 const ctx={URL,AbortSignal,crypto:require('node:crypto').webcrypto,
  setTimeout:fn=>{timer=fn;return 1;},clearTimeout:()=>{},
  fetch:async(url,options)=>{const body=JSON.parse(options.body);posts.push({url,body});if(fail)throw Error('offline');
   return{ok:true,json:async()=>url.endsWith('/hello')?{session:'local-session'}:url.endsWith('/poll')?{jobs:[],caption_jobs:[],dubbing_jobs:queue.splice(0)}:{accepted:true}};},
  chrome:{runtime:{id:'desktop-extension',getManifest:()=>({name:'Trancy 桌面字幕连接器',version:'0.1.5'}),
   getPlatformInfo:async()=>({os:'win'}),onMessage:{addListener:fn=>{listener=fn;}}},
   tabs:{sendMessage:async(tabId,message,options)=>{commands.push({tabId,message,options});return{accepted:true};},
    onRemoved:{addListener:()=>{}},onUpdated:{addListener:()=>{}}},alarms:{onAlarm:{addListener:()=>{}},create:()=>{}}}};
 ctx.self=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync('assets/desktop-bridge-background.js','utf8'),ctx);
 ctx.TrancyDesktopBridge.start({getState:async()=>({})});
 const video={platform:'bilibili',video_id:'BV1xx411c7mD',url:'https://www.bilibili.com/video/BV1xx411c7mD/?p=2',
  page:2,cid:'12345',dubbing_protocol:1,position:5,duration:120,paused:false,rate:1,document_start:100};
 const sender={id:'desktop-extension',frameId:0,tab:{id:10,url:video.url,active:true}};
 return{posts,commands,queue,video,sender,emit:m=>listener(m,sender),tick:async()=>{await timer();await flush();},offline:()=>{fail=true;}};
}
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the extension sandbox")
class DubbingBackgroundTests(unittest.TestCase):
    def run_node(self, body):
        result = subprocess.run([shutil.which("node"), "-e", BACKGROUND_SETUP + "\n(async()=>{\n" + body +
                                "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pause_event_is_uploaded_before_periodic_tick_and_keeps_dubbing_metadata(self):
        self.run_node(r'''
const bridge=setup();await flush();bridge.emit({kind:'trancy-desktop-report-v1',href:bridge.video.url,
 document_start:100,sequence:1,video:{...bridge.video,paused:true,buffering:true,seeking:true,seek_serial:3,playback_event:'seeking'}});
const report=bridge.posts.find(p=>p.url.endsWith('/video'));
assert.ok(report);assert.equal(report.body.video.paused,true);assert.equal(report.body.video.seek_serial,3);
assert.equal(report.body.video.buffering,true);assert.equal(report.body.video.dubbing_protocol,1);assert.equal(report.body.tab_id,10);
''')

    def test_end_state_survives_immediate_and_periodic_uploads_and_clears_on_manual_pause(self):
        self.run_node(r'''
const bridge=setup();await flush();let sequence=0;
for(const [ended,event] of [[true,'ended'],[true,''],[false,'pause'],['true','pause'],[undefined,'pause']]){
 bridge.emit({kind:'trancy-desktop-report-v1',href:bridge.video.url,document_start:100,sequence:++sequence,
  video:{...bridge.video,position:120,paused:true,ended,playback_event:event}});
 await bridge.tick();const report=bridge.posts.filter(p=>p.url.endsWith('/video')).at(-1);
 assert.ok(report);assert.equal(report.body.video.ended,ended===true);assert.equal(report.body.video.paused,true);
 assert.equal(report.body.video.playback_event,event);
}
''')

    def test_dubbing_ack_forwards_strict_end_state_without_losing_pause_or_seek(self):
        self.run_node(r'''
const bridge=setup();await flush();bridge.emit({kind:'trancy-desktop-report-v1',href:bridge.video.url,document_start:100,sequence:1,video:bridge.video});
let sequence=1;
for(const ended of [true,false,'true',undefined]){
 const job={id:String(++sequence).repeat(32),lease_id:'b'.repeat(32),tab_id:10,document_start:100,video:bridge.video,enabled:true};
 bridge.queue.push(job);await bridge.tick();bridge.emit({kind:'trancy-desktop-dubbing-report-v1',href:bridge.video.url,
  document_start:100,sequence,request_id:job.id,lease_id:job.lease_id,video:bridge.video,status:'active',
  position:120,paused:true,ended,seeking:true,seek_serial:2});await flush();
 const ack=bridge.posts.filter(p=>p.url.endsWith('/dubbing')).at(-1);
 assert.ok(ack);assert.equal(ack.body.ended,ended===true);assert.equal(ack.body.paused,true);
 assert.equal(ack.body.seeking,true);assert.equal(ack.body.seek_serial,2);
}
''')

    def test_selected_tab_control_ack_and_disconnect_release(self):
        self.run_node(r'''
const bridge=setup();await flush();bridge.emit({kind:'trancy-desktop-report-v1',href:bridge.video.url,document_start:100,sequence:1,video:bridge.video});
const job={id:'a'.repeat(32),lease_id:'b'.repeat(32),tab_id:10,document_start:100,video:bridge.video,enabled:true};
bridge.queue.push(job);await bridge.tick();assert.equal(bridge.commands.length,1);assert.equal(bridge.commands[0].tabId,10);
assert.equal(bridge.commands[0].message.kind,'trancy-desktop-dubbing-request-v1');assert.equal(bridge.commands[0].options.frameId,0);
bridge.emit({kind:'trancy-desktop-dubbing-report-v1',href:bridge.video.url,document_start:100,sequence:2,
 request_id:job.id,lease_id:job.lease_id,video:bridge.video,status:'active',position:5,paused:false,seek_serial:2,token:'secret'});await flush();
const ack=bridge.posts.find(p=>p.url.endsWith('/dubbing'));assert.ok(ack);assert.equal(ack.body.status,'active');assert.equal(ack.body.seek_serial,2);
assert.equal(JSON.stringify(ack.body).includes('secret'),false);bridge.offline();await bridge.tick();
assert.equal(bridge.commands.at(-1).message.enabled,false);assert.equal(bridge.commands.at(-1).message.reason,'bridge_disconnected');
''')

    def test_old_page_or_other_cid_command_is_rejected_before_send_message(self):
        self.run_node(r'''
for(const badVideo of [{dubbing_protocol:0},{cid:'99999'}]){
 const bridge=setup();await flush();bridge.emit({kind:'trancy-desktop-report-v1',href:bridge.video.url,document_start:100,sequence:1,
  video:{...bridge.video,...('dubbing_protocol'in badVideo?badVideo:{})}});
 bridge.queue.push({id:'a'.repeat(32),lease_id:'b'.repeat(32),tab_id:10,document_start:100,
  video:{...bridge.video,...('cid'in badVideo?badVideo:{})},enabled:true});await bridge.tick();
 assert.equal(bridge.commands.length,0);const ack=bridge.posts.find(p=>p.url.endsWith('/dubbing'));assert.equal(ack.body.status,'error');
}
''')


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the extension sandbox")
class DubbingContentTests(unittest.TestCase):
    def test_content_relay_preserves_end_in_periodic_video_reports_and_dubbing_ack(self):
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let runtimeListener,windowListener;const sent=[];
const ctx={performance:{timeOrigin:100},location:new URL('https://www.bilibili.com/video/BV1xx411c7mD/?p=2'),setTimeout:()=>{},
 chrome:{runtime:{id:'desktop-extension',onMessage:{addListener:fn=>{runtimeListener=fn;}},sendMessage:async m=>{sent.push(m);}}},
 addEventListener:(_,fn)=>{windowListener=fn;},postMessage:()=>{}};
ctx.window=ctx;ctx.top=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync('assets/desktop-bridge-content.js','utf8'),ctx);
ctx.__windowListener=windowListener;vm.runInContext(`__deliver=m=>__windowListener({source:window,origin:location.origin,data:m});`,ctx);
const video={platform:'bilibili',video_id:'BV1xx411c7mD',url:ctx.location.href,page:2,cid:'12345'};let sequence=0;
for(const ended of [true,false,'true',undefined]){
 const command={kind:'trancy-desktop-dubbing-request-v1',request_id:String(++sequence).repeat(32),
  lease_id:'b'.repeat(32),video,document_start:100,enabled:true};
 runtimeListener(command,{id:'desktop-extension'},()=>{});
 ctx.__deliver({kind:'trancy-desktop-dubbing-v1',href:ctx.location.href,sequence,document_start:100,request_id:command.request_id,
  lease_id:command.lease_id,video,status:'active',position:120,paused:true,ended,rate:1,seeking:true,seek_serial:2});
 const ack=sent.at(-1);assert.equal(ack.kind,'trancy-desktop-dubbing-report-v1');
 assert.equal(ack.ended,ended===true);assert.equal(ack.paused,true);assert.equal(ack.seeking,true);assert.equal(ack.seek_serial,2);
}
for(const [ended,playback_event] of [[true,'ended'],[true,''],[false,'pause']]){
 ctx.__deliver({kind:'trancy-desktop-video-v1',href:ctx.location.href,sequence:++sequence,document_start:100,
  video:{...video,position:120,paused:true,ended,playback_event}});
 const report=sent.at(-1);assert.equal(report.kind,'trancy-desktop-report-v1');
 assert.equal(report.video.ended,ended);assert.equal(report.video.paused,true);assert.equal(report.video.playback_event,playback_event);
}
'''
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_authorized_lease_can_relay_active_and_unsolicited_terminal_status(self):
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let runtimeListener,windowListener;const posted=[],sent=[];
const ctx={performance:{timeOrigin:100},location:new URL('https://www.bilibili.com/video/BV1xx411c7mD/?p=2'),setTimeout:()=>{},
 chrome:{runtime:{id:'desktop-extension',onMessage:{addListener:fn=>{runtimeListener=fn;}},sendMessage:async m=>{sent.push(m);}}},
 addEventListener:(_,fn)=>{windowListener=fn;},postMessage:m=>posted.push(m)};
ctx.window=ctx;ctx.top=ctx;
vm.createContext(ctx);vm.runInContext(fs.readFileSync('assets/desktop-bridge-content.js','utf8'),ctx);
ctx.__windowListener=windowListener;vm.runInContext(`__deliver=m=>__windowListener({source:window,origin:location.origin,data:m});`,ctx);
const video={platform:'bilibili',video_id:'BV1xx411c7mD',url:ctx.location.href,page:2,cid:'12345'};
const command={kind:'trancy-desktop-dubbing-request-v1',request_id:'a'.repeat(32),lease_id:'b'.repeat(32),video,document_start:100,enabled:true};
let reply;runtimeListener(command,{id:'other-extension'},r=>{reply=r;});assert.equal(posted.length,0);
runtimeListener(command,{id:'desktop-extension'},r=>{reply=r;});assert.equal(reply.accepted,true);assert.equal(posted[0].enabled,true);
const result={kind:'trancy-desktop-dubbing-v1',href:ctx.location.href,sequence:1,document_start:100,request_id:command.request_id,
 lease_id:command.lease_id,video,status:'active',position:5,paused:false,rate:1,seek_serial:2};
ctx.__deliver({...result,lease_id:'f'.repeat(32)});assert.equal(sent.length,0);
ctx.__deliver({...result,sequence:2});assert.equal(sent.length,1);assert.equal(sent[0].seek_serial,2);
ctx.__deliver({...result,sequence:3,status:'user_override',reason:'user_volume'});assert.equal(sent.length,2);assert.equal(sent[1].status,'user_override');
ctx.__deliver({...result,sequence:4});assert.equal(sent.length,2);
'''
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
