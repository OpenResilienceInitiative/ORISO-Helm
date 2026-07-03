import { test, expect, chromium, Browser } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { SignJWT } from 'jose';

/**
 * LiveKit SFU end-to-end verification.
 *
 * Two real Chromium participants mint access tokens with the LiveKit server API
 * key, connect to the deployed SFU over WSS, join the same room, each publish a
 * live video track (generated in-page via canvas.captureStream, so no camera
 * hardware is required), and assert that each participant subscribes to the
 * other's track. This exercises the full signalling + media path of the SFU.
 *
 * It is the regression guard for the 2026-07-03 crash-loop
 * (see docs/livekit-crashloop-postmortem-2026-07-03.md): a mis-configured SFU
 * either fails to start or accepts signalling but never carries media.
 *
 * Configuration comes exclusively from environment variables — NO secrets are
 * committed:
 *   LIVEKIT_WS_URL     e.g. wss://oriso-dev.site/livekit/sfu  (or wss://livekit.<domain>)
 *   LIVEKIT_API_KEY    server API key      (values: livekit.api.key)
 *   LIVEKIT_API_SECRET server API secret   (values: livekit.api.secret)
 *   LIVEKIT_ROOM       optional room name  (default: helm-ci-verify)
 */

const WSS_URL = process.env.LIVEKIT_WS_URL || '';
const API_KEY = process.env.LIVEKIT_API_KEY || '';
const API_SECRET = process.env.LIVEKIT_API_SECRET || '';
const ROOM = process.env.LIVEKIT_ROOM || 'helm-ci-verify';

// livekit-client UMD build, resolved from the local node_modules and inlined
// into each page so there is no external network load.
function resolveLivekitUmd(): string {
	const rel = ['livekit-client', 'dist', 'livekit-client.umd.js'];
	try {
		return require.resolve('livekit-client/dist/livekit-client.umd.js');
	} catch {
		// Fall back to common node_modules locations (sibling of the spec, or a
		// hoisted parent) so the test runs regardless of install layout.
		const candidates = [
			path.join(__dirname, 'node_modules', ...rel),
			path.join(__dirname, '..', 'node_modules', ...rel),
			path.join(__dirname, '..', '..', 'node_modules', ...rel)
		];
		const found = candidates.find((p) => fs.existsSync(p));
		if (found) return found;
		throw new Error(
			'Could not locate livekit-client UMD build. Run `npm install` in tests/livekit.'
		);
	}
}
const LK_UMD = fs.readFileSync(resolveLivekitUmd(), 'utf8');

async function mintToken(identity: string, name: string): Promise<string> {
	const secret = new TextEncoder().encode(API_SECRET);
	const now = Math.floor(Date.now() / 1000);
	return await new SignJWT({
		name,
		video: {
			room: ROOM,
			roomJoin: true,
			canPublish: true,
			canSubscribe: true,
			canPublishData: true
		}
	})
		.setProtectedHeader({ alg: 'HS256' })
		.setIssuer(API_KEY)
		.setSubject(identity)
		.setJti(identity)
		.setIssuedAt(now)
		.setNotBefore(now)
		.setExpirationTime(now + 600)
		.sign(secret);
}

const PAGE_HTML = `<!doctype html><html><head><meta charset="utf-8"></head>
<body><h1 id="s">idle</h1><script>__LK_UMD__</script>
<script>
window.__lk = { connected:false, remotes:0, remoteTracks:0, error:null, sub:0, log:[], roomName:null, published:false };
window.joinRoom = async function(url, token) {
  const LK = window.LivekitClient || window.LiveKitClient || window.livekit;
  const room = new LK.Room({ adaptiveStream:true, dynacast:true });
  window.__room = room;
  const recount = () => {
    const rp = room.remoteParticipants || room.participants;
    window.__lk.remotes = rp ? rp.size : 0;
    let t = 0;
    rp && rp.forEach(p => { t += (p.trackPublications ? p.trackPublications.size : (p.tracks ? p.tracks.size : 0)); });
    window.__lk.remoteTracks = t;
  };
  room.on(LK.RoomEvent.ParticipantConnected, (p) => { window.__lk.log.push('pc:' + (p && p.identity)); recount(); });
  room.on(LK.RoomEvent.TrackSubscribed, () => { window.__lk.sub++; recount(); });
  room.on(LK.RoomEvent.TrackPublished, recount);
  room.on(LK.RoomEvent.Disconnected, (r) => { window.__lk.connected = false; window.__lk.log.push('disc:' + r); });
  window.__snap = function () {
    const rp = room.remoteParticipants || room.participants;
    const ids = []; rp && rp.forEach((p) => ids.push(p.identity));
    return { name: room.name, state: room.state, remotes: rp ? rp.size : 0, remoteIds: ids };
  };
  try {
    await room.connect(url, token);
    window.__lk.connected = true;
    window.__lk.roomName = room.name;
    document.getElementById('s').textContent = 'connected ' + room.localParticipant.identity;
    recount();
    // Generate a real media track in-page via canvas.captureStream (no camera
    // hardware / getUserMedia needed) and publish it through the SFU.
    const canvas = document.createElement('canvas');
    canvas.width = 320; canvas.height = 240;
    const cctx = canvas.getContext('2d');
    let hue = 0;
    setInterval(() => {
      hue = (hue + 12) % 360;
      cctx.fillStyle = 'hsl(' + hue + ',80%,50%)';
      cctx.fillRect(0, 0, 320, 240);
      cctx.fillStyle = '#000';
      cctx.font = '28px sans-serif';
      cctx.fillText(room.localParticipant.identity + ' ' + Date.now(), 10, 120);
    }, 66);
    const stream = canvas.captureStream(15);
    const track = stream.getVideoTracks()[0];
    await room.localParticipant.publishTrack(track, { name: 'canvas', source: LK.Track.Source.Camera });
    window.__lk.published = true;
    recount();
  } catch (e) {
    window.__lk.error = String(e && e.message ? e.message : e);
    document.getElementById('s').textContent = 'ERROR ' + window.__lk.error;
  }
};
</script></body></html>`;

test('LiveKit SFU carries a two-participant WebRTC session', async () => {
	test.setTimeout(120_000);

	// Fail fast with a clear message if the environment is not configured.
	expect(WSS_URL, 'LIVEKIT_WS_URL must be set').toBeTruthy();
	expect(API_KEY, 'LIVEKIT_API_KEY must be set').toBeTruthy();
	expect(API_SECRET, 'LIVEKIT_API_SECRET must be set').toBeTruthy();

	const html = PAGE_HTML.replace('__LK_UMD__', () => LK_UMD);

	// Dedicated browser: media is generated via canvas.captureStream, so no fake
	// device is strictly required, but keep autoplay unrestricted.
	const browser: Browser = await chromium.launch({
		args: ['--autoplay-policy=no-user-gesture-required']
	});

	const mk = async () => {
		const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
		const page = await ctx.newPage();
		page.on('console', (m) => {
			if (m.type() === 'error') console.log('[page-err]', m.text().slice(0, 200));
		});
		await page.route('https://sfu-test.local/', (route) =>
			route.fulfill({ status: 200, contentType: 'text/html', body: html })
		);
		await page.goto('https://sfu-test.local/', { waitUntil: 'domcontentloaded' });
		return { ctx, page };
	};

	const a = await mk();
	const b = await mk();

	const tokenA = await mintToken('ci-A', 'Participant A');
	const tokenB = await mintToken('ci-B', 'Participant B');

	const hasSdk = await a.page.evaluate(
		() => !!(window as any).LivekitClient || !!(window as any).LiveKitClient
	);
	expect(hasSdk, 'livekit-client SDK loaded in page').toBeTruthy();

	await a.page.evaluate(([u, t]) => {
		(window as any).joinRoom(u, t);
	}, [WSS_URL, tokenA]);
	await b.page.evaluate(([u, t]) => {
		(window as any).joinRoom(u, t);
	}, [WSS_URL, tokenB]);

	const poll = async (page: typeof a.page) =>
		await page.evaluate(() => {
			const snap = (window as any).__snap ? (window as any).__snap() : {};
			return { ...(window as any).__lk, snap };
		});

	let stA: any = {};
	let stB: any = {};
	const deadline = Date.now() + 45_000;
	while (Date.now() < deadline) {
		stA = await poll(a.page);
		stB = await poll(b.page);
		const aSees = Math.max(stA.remotes || 0, stA.snap?.remotes || 0);
		const bSees = Math.max(stB.remotes || 0, stB.snap?.remotes || 0);
		if (stA.connected && stB.connected && aSees >= 1 && bSees >= 1 && stA.sub >= 1 && stB.sub >= 1)
			break;
		await a.page.waitForTimeout(1500);
	}

	console.log('A state:', JSON.stringify(stA));
	console.log('B state:', JSON.stringify(stB));

	const aSees = Math.max(stA.remotes || 0, stA.snap?.remotes || 0);
	const bSees = Math.max(stB.remotes || 0, stB.snap?.remotes || 0);
	expect(stA.error, 'A should have no connect error').toBeFalsy();
	expect(stB.error, 'B should have no connect error').toBeFalsy();
	expect(stA.connected, 'A connected to SFU').toBeTruthy();
	expect(stB.connected, 'B connected to SFU').toBeTruthy();
	expect(stA.roomName, 'A and B in same room').toBe(stB.roomName);
	expect(aSees, 'A sees participant B').toBeGreaterThanOrEqual(1);
	expect(bSees, 'B sees participant A').toBeGreaterThanOrEqual(1);
	expect(stA.sub, 'A subscribed to at least one of B tracks').toBeGreaterThanOrEqual(1);
	expect(stB.sub, 'B subscribed to at least one of A tracks').toBeGreaterThanOrEqual(1);

	await a.ctx.close();
	await b.ctx.close();
	await browser.close();
});
