/* Auto edit (tutorials page): ask for a video, follow it while the bot writes the story, records the voice and
   makes the pictures, then put the MP4 together right here in the browser (WebCodecs + mp4-muxer), so the
   server never does the heavy video work. */
(function () {
  'use strict';
  var root = document.getElementById('ae');
  if (!root) return;
  var $ = function (id) { return document.getElementById(id); };
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function api(path, body) {
    var opt = body ? { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
                   : { credentials: 'same-origin', cache: 'no-store' };
    return fetch(path, opt).then(function (r) {
      if (r.status === 401) { location.href = '/members/login'; throw 0; }
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || !j.ok) throw new Error(j.msg || 'Something went wrong. Try again.');
        return j;
      });
    });
  }
  function errText(e) {
    var m = e && e.message || '';
    return !m || m.indexOf('Failed to fetch') > -1 || m.indexOf('NetworkError') > -1 ? 'Could not reach the server. Check your connection and try again.' : m;
  }

  /* ---------------- the form ---------------- */
  var form = $('ae-form'), go = $('ae-go'), msg = $('ae-msg'), left = $('ae-left');
  var state = { left: +root.getAttribute('data-left'), limit: +root.getAttribute('data-limit'),
                open: root.getAttribute('data-open') === '1', window: root.getAttribute('data-window') };
  var clips = [];
  function say(t) { msg.textContent = t || ''; }
  function radio(name) { var r = form.querySelector('input[name=' + name + ']:checked'); return r ? r.value : ''; }
  function setRadio(name, v) { var r = form.querySelector('input[name=' + name + '][value="' + v + '"]'); if (r) r.checked = true; }
  function remember() {
    store('aeForm', JSON.stringify({ niche: $('ae-niche').value, minutes: radio('ae-min'), ratio: radio('ae-ratio'),
      language: $('ae-lang').value, voice: $('ae-voice').value }));
  }
  try {
    var saved = JSON.parse(store('aeForm') || '{}');
    if (saved.niche && $('ae-niche').querySelector('option[value="' + saved.niche + '"]')) $('ae-niche').value = saved.niche;
    if (saved.minutes) setRadio('ae-min', saved.minutes);
    if (saved.ratio) setRadio('ae-ratio', saved.ratio);
    if (saved.language) $('ae-lang').value = saved.language;
    if (saved.voice) $('ae-voice').value = saved.voice;
  } catch (e) {}
  ['ae-niche', 'ae-lang', 'ae-voice'].forEach(function (id) { $(id).addEventListener('change', remember); });
  Array.prototype.forEach.call(form.querySelectorAll('input[type=radio]'), function (r) { r.addEventListener('change', remember); });
  ['ae-concept', 'ae-script'].forEach(function (id) {
    var t = $(id); t.value = store(id) || ''; t.addEventListener('input', function () { store(id, t.value); });
    t.addEventListener('contextmenu', function (e) { e.stopPropagation(); });
  });
  $('ae-clips').addEventListener('change', function () {
    clips = Array.prototype.slice.call(this.files || []).filter(function (f) { return /^video\//.test(f.type) || /\.(mp4|mov|webm|m4v)$/i.test(f.name); }).slice(0, 3);
    $('ae-cliplist').textContent = clips.length ? clips.map(function (f) { return f.name; }).join(', ') + (this.files.length > 3 ? ' (first 3 only)' : '') : '';
  });
  function showLeft() {
    if (!state.open) { left.textContent = 'You can start videos ' + (state.window === 'Any time' ? 'any time' : 'from ' + state.window) + ' (Manila time).'; }
    else if (state.left <= 0) { left.textContent = 'No videos left today. Come back tomorrow for more.'; }
    else { left.textContent = state.left + ' of ' + state.limit + ' videos left today' + (state.window !== 'Any time' ? '. Your time: ' + state.window : ''); }
    go.disabled = busy || !state.open || state.left <= 0;
  }

  /* ---------------- following a video ---------------- */
  var card = $('ae-card'), steps = ['writing', 'voicing', 'drawing', 'music', 'build'], cur = null, poll = null, busy = false,
      autoBuild = {}, built = {};
  function setSteps(active, frac) {
    var at = steps.indexOf(active);
    Array.prototype.forEach.call($('ae-steps').children, function (li) {
      var i = steps.indexOf(li.getAttribute('data-s'));
      li.className = at < 0 ? (active === 'done' ? 'done' : '') : i < at ? 'done' : i === at ? 'now' : '';
    });
    var whole = active === 'done' ? 1 : at < 0 ? 0 : (at + Math.max(0, Math.min(1, frac || 0))) / steps.length;
    $('ae-bar').style.width = (whole * 100).toFixed(1) + '%';
  }
  function note(t) { $('ae-note').textContent = t || ''; }
  function showJob(j) {
    cur = j; card.hidden = false; $('ae-empty').hidden = true;
    $('ae-title').textContent = j.title || 'Your video';
    $('ae-meta').textContent = [j.niche, j.minutes + ' min', j.ratio, j.language].join(', ');
    $('ae-cancel').hidden = !j.active;
    $('ae-build').hidden = true; $('ae-again').hidden = true;
    if (j.active) {
      $('ae-result').hidden = true;
      var frac = j.total ? j.done / j.total : 0;
      setSteps(j.status === 'queued' ? '' : j.status, frac);
      note(j.status === 'queued' ? (j.line > 1 ? 'Waiting in line: number ' + j.line + '. Another member\'s video is being made first.' : 'Starting...')
        : j.label + (j.total > 1 ? ': ' + j.done + ' of ' + j.total : '') + '. You can leave this tab open and do something else.');
    } else if (j.status === 'ready') {
      if (!built[j.id]) {
        setSteps('build', 0); $('ae-result').hidden = true;
        if (autoBuild[j.id]) { delete autoBuild[j.id]; build(j.id); }
        else { $('ae-build').hidden = false; note('Everything is ready. Put the video together to get the MP4.'); }
      }
    } else {
      setSteps('', 0); $('ae-result').hidden = true;
      note(j.status === 'expired' ? 'The files of this video were cleared after a day. You can make it again with the same story.'
        : j.status === 'canceled' ? 'Canceled.' : (j.msg || 'This video didn\'t finish.'));
      $('ae-again').hidden = !j.again;
    }
  }
  function renderList(jobs) {
    var ul = $('ae-list'); ul.textContent = '';
    jobs.forEach(function (j) {
      var li = document.createElement('li'), b = document.createElement('button');
      b.type = 'button'; b.className = 'ae-item' + (cur && cur.id === j.id ? ' on' : '');
      var t = document.createElement('b'); t.textContent = j.title || j.niche;
      var s = document.createElement('span'); s.textContent = j.niche + ', ' + j.minutes + ' min, ' + j.when;
      var st = document.createElement('em'); st.className = 'st ' + (j.status === 'ready' ? 'ok' : j.active ? 'run' : 'off');
      st.textContent = j.active ? (j.status === 'queued' ? 'In line' : 'Making') : j.label;
      b.appendChild(t); b.appendChild(s); b.appendChild(st);
      b.addEventListener('click', function () { follow(j.id, false); });
      li.appendChild(b); ul.appendChild(li);
    });
    $('ae-list-wrap').hidden = !jobs.length;
  }
  function refresh() {
    var want = cur ? cur.id : '';
    return api('/watch/autoedit/jobs' + (want ? '?id=' + want : '')).then(function (r) {
      state.left = r.left; state.limit = r.limit; state.open = r.open; state.window = r.window; showLeft();
      renderList(r.jobs);
      var j = null;
      r.jobs.forEach(function (x) { if (cur && x.id === cur.id) j = x; });
      if (!j && !cur && r.jobs.length) j = r.jobs[0];
      if (j && !(building && cur && cur.id === j.id)) showJob(j);
      var active = r.jobs.some(function (x) { return x.active; });
      clearTimeout(poll); if (active) poll = setTimeout(refresh, 3000);
    }).catch(function (e) { if (e !== 0) { clearTimeout(poll); poll = setTimeout(refresh, 8000); } });
  }
  function follow(id, auto) {
    if (building) return;
    if (auto) autoBuild[id] = true;
    cur = { id: id }; refresh();
  }
  document.addEventListener('viewchange', function (e) { if (e.detail === 'auto') refresh(); });
  if (document.body.classList.contains('v-auto')) refresh();

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (busy) return;
    say('');
    var body = { niche: $('ae-niche').value, minutes: radio('ae-min'), ratio: radio('ae-ratio'), language: $('ae-lang').value,
                 voice: $('ae-voice').value, concept: $('ae-concept').value.trim(), script: $('ae-script').value.trim() };
    busy = true; go.disabled = true; go.textContent = 'Starting...';
    api('/watch/autoedit/start', body).then(function (r) {
      autoBuild[r.job.id] = true; cur = r.job; showJob(r.job); refresh();
      if (window.innerWidth < 900) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }).catch(function (err) { if (err !== 0) say(errText(err)); })
      .then(function () { busy = false; go.textContent = 'Make my video'; showLeft(); });
  });
  $('ae-cancel').addEventListener('click', function () {
    if (!cur || !confirm('Cancel this video? It won\'t count toward today\'s limit.')) return;
    api('/watch/autoedit/act', { id: cur.id, action: 'cancel' }).then(function (r) { showJob(r.job); refresh(); }).catch(function (e) { if (e !== 0) note(errText(e)); });
  });
  $('ae-again').addEventListener('click', function () {
    if (!cur) return;
    api('/watch/autoedit/act', { id: cur.id, action: 'again' }).then(function (r) { autoBuild[r.job.id] = true; showJob(r.job); refresh(); })
      .catch(function (e) { if (e !== 0) note(errText(e)); });
  });
  $('ae-build').addEventListener('click', function () { if (cur) build(cur.id); });
  $('ae-new').addEventListener('click', function () {
    $('ae-result').hidden = true; card.hidden = true; $('ae-empty').hidden = false; cur = null;
    form.scrollIntoView({ behavior: 'smooth', block: 'start' }); $('ae-niche').focus();
  });
  showLeft();

  /* ---------------- putting the video together ---------------- */
  var FPS = 30, XF = 0.6, GAP = 0.35, LEAD = 0.3, TAIL = 1.2, building = false, lastUrl = '';
  function loadMuxer() {
    if (window.Mp4Muxer) return Promise.resolve();
    return new Promise(function (ok, bad) {
      var s = document.createElement('script'); s.src = '/static/mp4-muxer.js'; s.onload = ok;
      s.onerror = function () { bad(new Error('Could not load the video maker. Refresh the page and try again.')); };
      document.head.appendChild(s);
    });
  }
  function yieldUI() { return new Promise(function (r) { var c = new MessageChannel(); c.port1.onmessage = function () { r(); }; c.port2.postMessage(0); }); }
  function waitQueue(enc) {
    return new Promise(function (r) { if ('ondequeue' in enc) enc.addEventListener('dequeue', function () { r(); }, { once: true }); else setTimeout(r, 4); });
  }
  function getBuf(url) {
    return fetch(url, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error(r.status === 404 ? 'The files of this video are gone. Make it again.' : 'Could not download the video files. Try again.');
      return r.arrayBuffer();
    });
  }
  function decode(buf, sr) {
    var C = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    var ctx = new C(1, 1, sr);
    return new Promise(function (ok, bad) {
      var p = ctx.decodeAudioData(buf.slice(0), ok, function () { bad(new Error('Could not read the voice files.')); });
      if (p && p.then) p.then(ok, function () { bad(new Error('Could not read the voice files.')); });
    }).then(function (ab) {
      var out = new Float32Array(ab.length), chs = ab.numberOfChannels;
      for (var c = 0; c < chs; c++) { var d = ab.getChannelData(c); for (var i = 0; i < d.length; i++) out[i] += d[i] / chs; }
      return out;
    });
  }
  function pickVideo(W, H) {
    var codecs = ['avc1.640028', 'avc1.4d0028', 'avc1.640033', 'avc1.42e028', 'avc1.42001f', 'vp09.00.40.08', 'vp09.00.31.08'];
    var sizes = [[W, H], [Math.round(W * 2 / 3 / 2) * 2, Math.round(H * 2 / 3 / 2) * 2]];
    var tries = [];
    sizes.forEach(function (s) { codecs.forEach(function (c) { tries.push({ codec: c, width: s[0], height: s[1], bitrate: s[0] * s[1] > 1500000 ? 3000000 : 1800000, framerate: FPS, avc: { format: 'avc' } }); }); });
    var i = 0;
    function next() {
      if (i >= tries.length) return Promise.resolve(null);
      var cfg = tries[i++];
      return VideoEncoder.isConfigSupported(cfg).then(function (r) { return r.supported ? cfg : next(); }, next);
    }
    return next();
  }
  function pickAudio() {
    var tries = [48000, 44100].map(function (sr) { return { codec: 'mp4a.40.2', sampleRate: sr, numberOfChannels: 1, bitrate: 128000 }; })
      .concat([{ codec: 'opus', sampleRate: 48000, numberOfChannels: 1, bitrate: 96000 }]);
    var i = 0;
    function next() {
      if (i >= tries.length) return Promise.resolve(null);
      var cfg = tries[i++];
      return AudioEncoder.isConfigSupported(cfg).then(function (r) { return r.supported ? cfg : next(); }, next);
    }
    return next();
  }
  function chunksOf(text, max) {
    var words = text.split(/\s+/).filter(Boolean), out = [], curw = [];
    words.forEach(function (w) {
      curw.push(w);
      if (curw.length >= max || (/[.!?;:]$/.test(w) && curw.length >= Math.ceil(max / 2))) { out.push(curw.join(' ')); curw = []; }
    });
    if (curw.length) out.push(curw.join(' '));
    return out;
  }
  function wrap(ctx, text, maxW) {
    var words = text.split(' '), lines = [], line = '';
    words.forEach(function (w) {
      var t = line ? line + ' ' + w : w;
      if (ctx.measureText(t).width > maxW && line) { lines.push(line); line = w; } else line = t;
    });
    if (line) lines.push(line);
    return lines;
  }
  function seek(v, t) {
    return new Promise(function (r) {
      if (Math.abs(v.currentTime - t) < 0.002) return r();
      var done = false, to = setTimeout(function () { if (!done) { done = true; r(); } }, 1500);
      v.addEventListener('seeked', function () { if (!done) { done = true; clearTimeout(to); r(); } }, { once: true });
      v.currentTime = t;
    });
  }
  function slug(t) { return (t || 'video').toLowerCase().replace(/[^a-z0-9\u00c0-\u024f]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'video'; }

  function build(id) {
    if (building) return;
    if (!(window.VideoEncoder && window.AudioEncoder && window.VideoFrame && window.AudioData)) {
      note('This browser can\'t put the video together. Open this page in Chrome or Edge (a computer works best).');
      $('ae-build').hidden = false; return;
    }
    building = true; $('ae-build').hidden = true; $('ae-result').hidden = true; $('ae-cancel').hidden = true;
    setSteps('build', 0); note('Getting ready...');
    var lock = null;
    try { if (navigator.wakeLock) navigator.wakeLock.request('screen').then(function (l) { lock = l; }, function () {}); } catch (e) {}
    var job, files = { voice: [], pics: [] }, music = null, venc, aenc, muxer, failed = null;
    api('/watch/autoedit/jobs?id=' + id).then(function (r) {
      r.jobs.forEach(function (x) { if (x.id === id) job = x; });
      if (!job || job.status !== 'ready' || !job.scenes) throw new Error('This video isn\'t ready anymore. Refresh and try again.');
      return loadMuxer();
    }).then(function () {
      var W = job.ratio === '9:16' ? 1080 : 1920, H = job.ratio === '9:16' ? 1920 : 1080;
      return Promise.all([pickVideo(W, H), pickAudio()]);
    }).then(function (cfgs) {
      var vcfg = cfgs[0], acfg = cfgs[1];
      if (!vcfg || !acfg) throw new Error('This browser can\'t make MP4 video with sound. Open this page in Chrome or Edge on a computer.');
      var SR = acfg.sampleRate, n = job.scenes.length, base = '/watch/autoedit/file/' + id + '/';
      var pad = function (i) { return ('00' + i).slice(-3); };
      /* 1. download everything (4 at a time) and measure each voice clip */
      var durs = new Array(n), got = 0, total = n * 2 + (job.music ? 1 : 0);
      function step() { got++; setSteps('build', 0.25 * got / total); note('Downloading the voice and pictures: ' + Math.round(100 * got / total) + '%'); }
      var tasks = [];
      for (var i = 0; i < n; i++) (function (i) {
        tasks.push(function () { return getBuf(base + 'v' + pad(i) + '.mp3').then(function (b) { files.voice[i] = b; return decode(b, SR); }).then(function (pcm) { durs[i] = pcm.length / SR; step(); }); });
        tasks.push(function () { return fetch(base + 'p' + pad(i) + '.jpg', { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error('Could not download the pictures. Try again.'); return r.blob(); }).then(function (b) { files.pics[i] = b; step(); }); });
      })(i);
      if (job.music) tasks.push(function () { return getBuf(base + 'music.mp3').then(function (b) { return decode(b, SR); }).then(function (pcm) { music = pcm; step(); }, function () { music = null; step(); }); });
      var k = 0;
      function worker() { if (k >= tasks.length) return Promise.resolve(); var t = tasks[k++]; return t().then(worker); }
      return Promise.all([worker(), worker(), worker(), worker()]).then(function () { return [vcfg, acfg, durs]; });
    }).then(function (x) {
      var vcfg = x[0], acfg = x[1], durs = x[2], SR = acfg.sampleRate;
      var W = vcfg.width, H = vcfg.height, tall = H > W, n = durs.length;
      /* 2. timeline */
      var sc = [], t = LEAD;
      for (var i = 0; i < n; i++) { sc.push({ start: t, dur: durs[i] }); t += durs[i] + GAP; }
      var total = t + TAIL, frames = Math.ceil(total * FPS);
      sc.forEach(function (s, i) {
        s.vs = i === 0 ? 0 : s.start - XF / 2;
        s.ve = i === n - 1 ? total : sc[i + 1].start + XF / 2;
        var parts = chunksOf(job.scenes[i].text, tall ? 6 : 9), lens = parts.map(function (p) { return p.length + 6; });
        var sum = lens.reduce(function (a, b) { return a + b; }, 0), at = s.start;
        s.subs = parts.map(function (p, j) { var d = s.dur * lens[j] / sum, o = { a: at, b: at + d, text: p }; at += d; return o; });
      });
      /* own clips take the place of the pictures of a few scenes spread over the video */
      var clipAt = {};
      clips.forEach(function (f, j) { var idx = Math.min(n - 1, Math.max(1, Math.round((j + 1) * n / (clips.length + 1)))); clipAt[idx] = f; });
      var clipVids = {};
      var prep = Object.keys(clipAt).map(function (idx) {
        return new Promise(function (ok) {
          var v = document.createElement('video'); v.muted = true; v.playsInline = true; v.preload = 'auto';
          v.onloadeddata = function () { clipVids[idx] = v; ok(); }; v.onerror = function () { ok(); };
          v.src = URL.createObjectURL(clipAt[idx]);
        });
      });
      return Promise.all(prep).then(function () { return run(); });

      function run() {
        var canvas = document.createElement('canvas'); canvas.width = W; canvas.height = H;
        var ctx = canvas.getContext('2d', { alpha: false });
        var target = new Mp4Muxer.ArrayBufferTarget();
        muxer = new Mp4Muxer.Muxer({ target: target, video: { codec: vcfg.codec.indexOf('vp09') === 0 ? 'vp9' : 'avc', width: W, height: H, frameRate: FPS },
          audio: { codec: acfg.codec === 'opus' ? 'opus' : 'aac', numberOfChannels: 1, sampleRate: SR }, fastStart: 'in-memory', firstTimestampBehavior: 'offset' });
        venc = new VideoEncoder({ output: function (c, m) { muxer.addVideoChunk(c, m); }, error: function (e) { failed = e; } });
        venc.configure(vcfg);
        aenc = new AudioEncoder({ output: function (c, m) { muxer.addAudioChunk(c, m); }, error: function (e) { failed = e; } });
        aenc.configure(acfg);
        var bitmaps = {}, voiceCache = {}, fontPx = Math.round((tall ? 0.034 : 0.05) * H), started = performance.now();
        ctx.font = '800 ' + fontPx + 'px "Instrument Sans", system-ui, -apple-system, "Segoe UI", sans-serif';
        function bitmap(i) {
          if (bitmaps[i]) return Promise.resolve(bitmaps[i]);
          return createImageBitmap(files.pics[i]).then(function (b) { bitmaps[i] = b; return b; });
        }
        function forget(before) { Object.keys(bitmaps).forEach(function (k) { if (+k < before) { try { bitmaps[k].close(); } catch (e) {} delete bitmaps[k]; } }); }
        function voice(i) { if (voiceCache[i]) return Promise.resolve(voiceCache[i]); return decode(files.voice[i], SR).then(function (p) { voiceCache[i] = p; return p; }); }
        function drawOne(i, tt, alpha) {
          var s = sc[i], p = Math.max(0, Math.min(1, (tt - s.vs) / Math.max(0.01, s.ve - s.vs)));
          ctx.globalAlpha = alpha;
          var v = clipVids[i];
          if (v) {
            var vw = v.videoWidth || W, vh = v.videoHeight || H, z = Math.max(W / vw, H / vh);
            ctx.drawImage(v, (W - vw * z) / 2, (H - vh * z) / 2, vw * z, vh * z);
          } else if (bitmaps[i]) {
            var zoom = i % 2 ? 1.16 - 0.12 * p : 1.04 + 0.12 * p;
            var dx = ((i % 3) - 1) * 0.03 * W * (p - 0.5), dy = (((i + 1) % 3) - 1) * 0.03 * H * (p - 0.5);
            var w = W * zoom, h = H * zoom;
            ctx.drawImage(bitmaps[i], (W - w) / 2 + dx, (H - h) / 2 + dy, w, h);
          }
          ctx.globalAlpha = 1;
        }
        function subtitle(tt) {
          for (var i = 0; i < n; i++) {
            var s = sc[i]; if (tt < s.start - 0.05 || tt > s.start + s.dur + 0.05) continue;
            for (var j = 0; j < s.subs.length; j++) {
              var c = s.subs[j]; if (tt < c.a || tt >= c.b) continue;
              var lines = wrap(ctx, c.text, W * (tall ? 0.84 : 0.8)), lh = fontPx * 1.22, y0 = H * (tall ? 0.7 : 0.86) - (lines.length - 1) * lh;
              ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
              ctx.lineWidth = Math.max(4, fontPx * 0.16); ctx.strokeStyle = 'rgba(0,0,0,0.92)'; ctx.fillStyle = '#fff';
              lines.forEach(function (ln, k) { ctx.strokeText(ln, W / 2, y0 + k * lh); ctx.fillText(ln, W / 2, y0 + k * lh); });
              return;
            }
          }
        }
        var secondsDone = 0, totalSamples = Math.ceil(total * SR);
        function audioSecond(sec) {
          /* mix one second: voice of the scenes playing now + the music under it, quieter */
          var a = sec * SR, len = Math.min(SR, totalSamples - a);
          if (len <= 0) return Promise.resolve();
          var buf = new Float32Array(len), need = [];
          for (var i = 0; i < n; i++) { var s0 = Math.round(sc[i].start * SR), s1 = s0 + Math.round(sc[i].dur * SR); if (s1 > a && s0 < a + len) need.push(i); }
          return Promise.all(need.map(voice)).then(function (pcms) {
            need.forEach(function (i, k) {
              var p = pcms[k], s0 = Math.round(sc[i].start * SR);
              for (var x = Math.max(0, s0 - a); x < len; x++) { var y = a + x - s0; if (y >= p.length) break; buf[x] += p[y]; }
            });
            Object.keys(voiceCache).forEach(function (k) { if (need.indexOf(+k) < 0 && +k < (need[0] || 0)) delete voiceCache[k]; });
            if (music && music.length) {
              for (var x = 0; x < len; x++) {
                var g = a + x, fade = Math.min(1, g / (SR * 2), (totalSamples - g) / (SR * 2.5));
                buf[x] += music[g % music.length] * 0.13 * Math.max(0, fade);
              }
            }
            for (var z = 0; z < len; z++) buf[z] = Math.max(-1, Math.min(1, buf[z]));
            var ad = new AudioData({ format: 'f32', sampleRate: SR, numberOfFrames: len, numberOfChannels: 1, timestamp: Math.round(a / SR * 1e6), data: buf });
            aenc.encode(ad); ad.close();
          });
        }
        var f = 0;
        function frame() {
          if (failed) return Promise.reject(failed);
          if (f >= frames) return Promise.resolve();
          var tt = f / FPS, list = [];
          for (var i = 0; i < n; i++) if (tt >= sc[i].vs && tt < sc[i].ve) list.push(i);
          var loads = list.map(function (i) {
            if (clipVids[i]) { var v = clipVids[i], d = v.duration || 1; return seek(v, (tt - sc[i].vs) % Math.max(0.1, d - 0.05)); }
            return bitmap(i).catch(function () {});
          });
          if (list.length && list[0] + 1 < n && !clipVids[list[0] + 1]) loads.push(bitmap(list[0] + 1).catch(function () {}));
          return Promise.all(loads).then(function () {
            ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H);
            list.forEach(function (i, k) {
              var alpha = 1;
              if (k > 0) alpha = Math.max(0, Math.min(1, (tt - sc[i].vs) / XF));
              drawOne(i, tt, alpha);
            });
            subtitle(tt);
            if (list.length) forget(list[0]);
            var vf = new VideoFrame(canvas, { timestamp: Math.round(tt * 1e6), duration: Math.round(1e6 / FPS) });
            venc.encode(vf, { keyFrame: f % (FPS * 2) === 0 }); vf.close();
            var more = Promise.resolve();
            if (f % FPS === 0) more = audioSecond(secondsDone++);
            f++;
            if (f % 15 === 0) {
              var pct = f / frames, el = (performance.now() - started) / 1000, rest = pct > 0.02 ? el / pct - el : 0;
              setSteps('build', 0.25 + 0.75 * pct);
              note('Putting the video together: ' + Math.round(pct * 100) + '%' + (rest > 5 ? ', about ' + (rest > 90 ? Math.round(rest / 60) + ' min' : Math.round(rest) + ' s') + ' left' : '') + '. Keep this tab open.');
            }
            return more.then(function () {
              var w = venc.encodeQueueSize > 8 ? waitQueue(venc) : (f % 10 === 0 ? yieldUI() : null);
              return w;
            });
          }).then(frame);
        }
        return frame().then(function () {
          var rest = [];
          while (secondsDone * SR < totalSamples) rest.push(secondsDone++);
          return rest.reduce(function (p, s) { return p.then(function () { return audioSecond(s); }); }, Promise.resolve());
        }).then(function () { return venc.flush(); }).then(function () { return aenc.flush(); }).then(function () {
          if (failed) throw failed;
          muxer.finalize();
          forget(1e9);
          Object.keys(clipVids).forEach(function (k) { try { URL.revokeObjectURL(clipVids[k].src); } catch (e) {} });
          return new Blob([target.buffer], { type: 'video/mp4' });
        });
      }
    }).then(function (blob) {
      if (lastUrl) URL.revokeObjectURL(lastUrl);
      lastUrl = URL.createObjectURL(blob);
      built[id] = true;
      var v = $('ae-video'); v.src = lastUrl;
      var dl = $('ae-dl'); dl.href = lastUrl; dl.setAttribute('download', slug(job.title) + '.mp4');
      $('ae-result').hidden = false; setSteps('done', 1);
      note('Done! ' + (blob.size / 1048576).toFixed(0) + ' MB. Download it before you close this page.');
    }).catch(function (e) {
      if (e === 0) return;
      note(errText(e) + ' ');
      $('ae-build').hidden = false; $('ae-build').textContent = 'Try again';
    }).then(function () {
      building = false;
      try { if (venc && venc.state !== 'closed') venc.close(); } catch (e) {}
      try { if (aenc && aenc.state !== 'closed') aenc.close(); } catch (e) {}
      try { if (lock) lock.release(); } catch (e) {}
      files = null; music = null;
      refresh();
    });
  }
})();
