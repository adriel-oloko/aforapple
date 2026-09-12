"use client";

// LiveRealtimeEditor
//
// Streams the user's webcam to a real-time AI model (via fal.ai) over
// WebRTC and renders the raw input feed next to the live edited output feed.
//
// Setup:
//   npm install @fal-ai/client @fal-ai/server-proxy
//   Add FAL_KEY to your server environment (.env.local)
//   Add the proxy route at app/api/fal/proxy/route.ts
//
// Usage:
//   import LiveRealtimeEditor from "@/components/LiveRealtimeEditor";
//   export default function Page() { return <LiveRealtimeEditor />; }

import { useCallback, useEffect, useRef, useState } from "react";
import { fal, type TokenProvider } from "@fal-ai/client";

// Fetches a short-lived JWT from our server so FAL_KEY never reaches the browser.
// The fal client calls this automatically and refreshes before expiry.
const tokenProvider: TokenProvider = async (app) => {
	const res = await fetch(`/api/fal/token?app=${encodeURIComponent(app)}`);
	if (!res.ok) throw new Error(`Token endpoint returned ${res.status}`);
	return res.text();
};

const MODEL_ENDPOINT = "decart/lucy-2-5/realtime";

// ── Session registry ─────────────────────────────────────────────────────────
//
// The model behind this endpoint (Decart) caps how many realtime sessions one
// account may hold at once. A session is counted the moment its connection is
// authenticated and released the moment it closes, so a connection that is
// never closed keeps burning a slot until the server reaps it -- and the next
// `fal.realtime.connect()` is rejected with WebSocket close code 1013,
// "Concurrent session limit reached."
//
// `fal.realtime.connect()` returns a handle, but the connection itself lives in
// the SDK's module-level cache, keyed by `connectionKey`. Storing just the
// newest handle in a ref therefore orphans every earlier connection: nothing
// can close them any more. They are registered here instead, so starting a new
// session can kill all of the previous ones first.
type RealtimeConnectionHandle = ReturnType<
	typeof fal.realtime.connect<LiveRealtimeInput>
>;

const openSessions = new Set<RealtimeConnectionHandle>();

// Other tabs of this app keep their own connections (and their own copy of the
// registry above), so the kill is broadcast to them as well.
const SESSION_KILL_CHANNEL = "live-editor-session-kill";

function closeSession(connection: RealtimeConnectionHandle) {
	openSessions.delete(connection);
	try {
		connection.close();
	} catch {
		// Already closed, or torn down by the SDK after a hard disconnect.
	}
}

/** Kills every realtime session this tab still has open. */
function killLocalSessions() {
	for (const connection of Array.from(openSessions)) closeSession(connection);
}

/** Asks every other tab of this app to kill its realtime sessions too. */
function broadcastKillSessions() {
	if (typeof window === "undefined" || !("BroadcastChannel" in window)) return;
	const channel = new BroadcastChannel(SESSION_KILL_CHANNEL);
	channel.postMessage({ type: "kill-sessions" });
	channel.close();
}

// Decart rejects the connect itself when the cap is hit (close code 1013, with
// the message "Concurrent session limit reached."). Its own docs note that
// sessions from a crashed gateway keep counting for up to ~45s, so the
// rejection is often transient and worth retrying rather than surfacing as a
// dead end.
function isConcurrencyLimitError(error: unknown): boolean {
	const message = error instanceof Error ? error.message : String(error);
	const status = (error as { status?: unknown } | null)?.status;
	return status === 1013 || /concurrent session limit/i.test(message);
}

const CONCURRENCY_RETRY_DELAYS_MS = [3000, 8000, 15000, 30000];

function wait(ms: number) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

type ConnectOutcome = "live" | "concurrency" | "error";

type ConnectionStatus =
	| "idle"
	| "requesting-camera"
	| "connecting"
	| "live"
	| "error";

interface RealtimeResult {
	type?: string;
	sdp?: string;
	candidate?: RTCIceCandidateInit;
	iceServers?: RTCIceServer[];
	iceservers?: RTCIceServer[];
	ice_servers?: RTCIceServer[];
	error?: string;
}

interface LiveRealtimeInput {
	prompt?: string;
	enable_prompt_expansion?: boolean;
	reference_image_url?: string | null;
	type?: "offer" | "icecandidate";
	sdp?: string;
	candidate?: {
		candidate: string;
		sdpMid: string | null;
		sdpMLineIndex: number | null;
	};
}

// ── SVG icons (inline, no extra dep) ────────────────────────────────────────

function IconExpand({ className }: { className?: string }) {
	return (
		<svg
			className={className}
			viewBox="0 0 16 16"
			fill="none"
			stroke="currentColor"
			strokeWidth="1.5">
			<path
				d="M10 2h4v4M6 14H2v-4M14 10v4h-4M2 6V2h4"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

function IconCollapse({ className }: { className?: string }) {
	return (
		<svg
			className={className}
			viewBox="0 0 16 16"
			fill="none"
			stroke="currentColor"
			strokeWidth="1.5">
			<path
				d="M6 2v4H2M10 14v-4h4M14 6h-4V2M2 10h4v4"
				strokeLinecap="round"
				strokeLinejoin="round"
			/>
		</svg>
	);
}

// ── Main component ───────────────────────────────────────────────────────────

export default function LiveRealtimeEditor() {
	const inputVideoRef = useRef<HTMLVideoElement>(null);
	const outputVideoRef = useRef<HTMLVideoElement>(null);

	// The output MediaStream outlives any single <video> element: the same ref
	// is bound to two different elements (main panel vs. expanded overlay) that
	// mount/unmount when the view toggles. Hold the stream here and re-attach
	// it to whichever element currently owns outputVideoRef.
	const outputStreamRef = useRef<MediaStream | null>(null);

	const localStreamRef = useRef<MediaStream | null>(null);
	const peerConnectionRef = useRef<RTCPeerConnection | null>(null);
	const connectionRef = useRef<ReturnType<
		typeof fal.realtime.connect<LiveRealtimeInput>
	> | null>(null);

	const [status, setStatus] = useState<ConnectionStatus>("idle");
	const [errorMessage, setErrorMessage] = useState<string | null>(null);
	const [prompt, setPrompt] = useState(
		"Substitute the character in the video with the person in the reference image",//. Maintain photorealistic skin texture, sharp facial features, and cinematic lighting. Output at maximum sharpness with no blur or artifacts.",
	);
	const [expanded, setExpanded] = useState(false);

	const [referenceImagePreview, setReferenceImagePreview] = useState<
		string | null
	>(null);
	const [referenceImageUrl, setReferenceImageUrl] = useState<string | null>(
		null,
	);
	const [isUploadingReference, setIsUploadingReference] = useState(false);

	// ── cleanup ────────────────────────────────────────────────────────────────

	// Bumped every time the live session is torn down. In-flight connects and
	// retry waits compare against it so a session that was stopped can never
	// resurrect itself (see `start`).
	const sessionGenerationRef = useRef(0);

	// Settled by the first terminal event of a connect attempt: the output
	// track arriving ("live") or an error. Lets `start` await an attempt and
	// decide whether the concurrency rejection is worth retrying.
	const connectOutcomeRef = useRef<((outcome: ConnectOutcome) => void) | null>(
		null,
	);

	// Every connect attempt takes a token and the newest one wins, so an error
	// arriving from an attempt that was already superseded (or from a session
	// the user stopped) cannot cancel the attempt that replaced it.
	const attemptTokenRef = useRef(0);

	const cleanup = useCallback(() => {
		sessionGenerationRef.current += 1;
		attemptTokenRef.current += 1;
		connectOutcomeRef.current = null;
		// Kill *every* connection this tab opened, not just the newest one: an
		// orphaned handle is unreachable and keeps holding one of Decart's
		// concurrent-session slots until the server times it out.
		killLocalSessions();
		peerConnectionRef.current?.close();
		peerConnectionRef.current = null;
		connectionRef.current = null;
		localStreamRef.current?.getTracks().forEach((t) => t.stop());
		localStreamRef.current = null;
		outputStreamRef.current = null;
		if (inputVideoRef.current) inputVideoRef.current.srcObject = null;
		if (outputVideoRef.current) outputVideoRef.current.srcObject = null;
	}, []);

	useEffect(() => () => cleanup(), [cleanup]);

	// Another tab is starting a session: only one may be live, so this tab
	// drops whatever it holds and returns to idle.
	useEffect(() => {
		if (typeof window === "undefined" || !("BroadcastChannel" in window))
			return;
		const channel = new BroadcastChannel(SESSION_KILL_CHANNEL);
		channel.onmessage = (event: MessageEvent) => {
			const message = event.data as { type?: string } | null;
			if (message?.type !== "kill-sessions") return;
			cleanup();
			setStatus("idle");
			setExpanded(false);
		};
		return () => {
			channel.onmessage = null;
			channel.close();
		};
	}, [cleanup]);

	// Refresh / navigate away: release the session now instead of leaving it
	// counted against the cap until the server reaps it.
	useEffect(() => {
		const releaseOnExit = () => {
			killLocalSessions();
			peerConnectionRef.current?.close();
		};
		window.addEventListener("pagehide", releaseOnExit);
		return () => window.removeEventListener("pagehide", releaseOnExit);
	}, []);

	useEffect(() => {
		return () => {
			if (referenceImagePreview)
				URL.revokeObjectURL(referenceImagePreview);
		};
	}, [referenceImagePreview]);

	// The output <video> element is swapped when the view toggles (the main
	// panel video unmounts and the overlay video mounts, or vice versa). The
	// freshly mounted element has no srcObject, so re-attach the saved stream
	// to whatever element owns outputVideoRef after each swap.
	useEffect(() => {
		if (outputStreamRef.current && outputVideoRef.current) {
			outputVideoRef.current.srcObject = outputStreamRef.current;
		}
	}, [expanded]);

	// Close expanded view with Escape
	useEffect(() => {
		if (!expanded) return;
		const handler = (e: KeyboardEvent) => {
			if (e.key === "Escape") setExpanded(false);
		};
		window.addEventListener("keydown", handler);
		return () => window.removeEventListener("keydown", handler);
	}, [expanded]);

	// ── WebRTC signaling handler ───────────────────────────────────────────────

	const handleResult = useCallback(async (result: RealtimeResult) => {
		const stream = localStreamRef.current;
		const connection = connectionRef.current;
		if (!stream || !connection) return;

		const msgType = (result.type ?? "").toLowerCase();

		try {
			if (msgType === "iceservers" && !peerConnectionRef.current) {
				const servers =
					result.iceServers ??
					result.iceservers ??
					result.ice_servers ??
					[];
				const pc = new RTCPeerConnection({ iceServers: servers });
				peerConnectionRef.current = pc;

				stream
					.getTracks()
					.forEach((track) => pc.addTrack(track, stream));

				pc.ontrack = (e) => {
					outputStreamRef.current = e.streams[0];
					if (outputVideoRef.current)
						outputVideoRef.current.srcObject = e.streams[0];
					setErrorMessage(null);
					setStatus("live");
					// The session is authenticated and producing frames: this
					// attempt is the one that stuck, so stop retrying.
					connectOutcomeRef.current?.("live");
					connectOutcomeRef.current = null;
				};

				pc.onicecandidate = (e) => {
					if (e.candidate) {
						connection.send({
							type: "icecandidate",
							candidate: {
								candidate: e.candidate.candidate,
								sdpMid: e.candidate.sdpMid,
								sdpMLineIndex: e.candidate.sdpMLineIndex,
							},
						});
					}
				};

				const offer = await pc.createOffer();
				await pc.setLocalDescription(offer);
				connection.send({ type: "offer", sdp: offer.sdp });
				return;
			}

			if (msgType === "answer" && peerConnectionRef.current) {
				await peerConnectionRef.current.setRemoteDescription({
					type: "answer",
					sdp: result.sdp,
				});
				return;
			}

			if (
				msgType === "icecandidate" &&
				peerConnectionRef.current &&
				result.candidate
			) {
				await peerConnectionRef.current.addIceCandidate(
					new RTCIceCandidate(result.candidate),
				);
				return;
			}

			if (result.error) {
				setErrorMessage(String(result.error));
				setStatus("error");
				connectOutcomeRef.current?.("error");
				connectOutcomeRef.current = null;
			}
		} catch (err) {
			setErrorMessage(err instanceof Error ? err.message : String(err));
			setStatus("error");
			connectOutcomeRef.current?.("error");
			connectOutcomeRef.current = null;
		}
	}, []);

	// ── Reference image upload ─────────────────────────────────────────────────

	const handleReferenceImageChange = useCallback(
		async (e: React.ChangeEvent<HTMLInputElement>) => {
			const file = e.target.files?.[0];
			e.target.value = "";
			if (!file) return;

			if (referenceImagePreview)
				URL.revokeObjectURL(referenceImagePreview);
			setReferenceImagePreview(URL.createObjectURL(file));
			setReferenceImageUrl(null);
			setIsUploadingReference(true);
			setErrorMessage(null);

			try {
				const fd = new FormData();
				fd.append("file", file);
				const res = await fetch("/api/upload-reference", {
					method: "POST",
					body: fd,
				});
				const { url } = await res.json();
				setReferenceImageUrl(url);
			} catch (err) {
				setErrorMessage(
					err instanceof Error
						? err.message
						: "Reference image upload failed",
				);
			} finally {
				setIsUploadingReference(false);
			}
		},
		[referenceImagePreview],
	);

	const clearReferenceImage = useCallback(() => {
		if (referenceImagePreview) URL.revokeObjectURL(referenceImagePreview);
		setReferenceImagePreview(null);
		setReferenceImageUrl(null);
	}, [referenceImagePreview]);

	// ── Session control ────────────────────────────────────────────────────────

	// Opens one realtime connection and resolves when the attempt reaches a
	// terminal state, so `start` can tell a transient concurrency rejection
	// apart from a genuine failure.
	const connectOnce = useCallback(
		() =>
			new Promise<ConnectOutcome>((resolve) => {
				const token = attemptTokenRef.current + 1;
				attemptTokenRef.current = token;

				const finish = (outcome: ConnectOutcome) => {
					if (connectOutcomeRef.current !== finish) return;
					connectOutcomeRef.current = null;
					resolve(outcome);
				};
				connectOutcomeRef.current = finish;

				let connection: RealtimeConnectionHandle | null = null;

				connection = fal.realtime.connect<LiveRealtimeInput>(
					MODEL_ENDPOINT,
					{
						connectionKey: `live-session-${Date.now()}`,
						tokenProvider,
						tokenExpirationSeconds: 120,
						onResult: handleResult,
						onError: (err) => {
							// Superseded attempt, or a session the user stopped:
							// its connection is already closed, so its errors are
							// not this attempt's problem.
							if (attemptTokenRef.current !== token) return;
							const message =
								err instanceof Error ? err.message : String(err);
							setErrorMessage(message);
							if (connection) closeSession(connection);
							if (isConcurrencyLimitError(err)) {
								finish("concurrency");
								return;
							}
							setStatus("error");
							finish("error");
						},
					},
				);
				openSessions.add(connection);
				connectionRef.current = connection;

				connection.send({
					prompt,
					enable_prompt_expansion: true,
					reference_image_url: referenceImageUrl ?? null,
				});
			}),
		[handleResult, prompt, referenceImageUrl],
	);

	const start = useCallback(async () => {
		setErrorMessage(null);
		// Kill everything still live before opening a new session. A connection
		// that was never closed keeps holding one of the account's concurrent
		// slots, which is exactly what makes the next start fail with
		// "Concurrent session limit reached."
		cleanup();
		broadcastKillSessions();

		setStatus("requesting-camera");

		const generation = sessionGenerationRef.current;
		const isCurrent = () => sessionGenerationRef.current === generation;

		try {
			const stream = await navigator.mediaDevices.getUserMedia({
				video: { width: { ideal: 1280 }, height: { ideal: 720 } },
				audio: false,
			});
			if (!isCurrent()) {
				stream.getTracks().forEach((track) => track.stop());
				return;
			}
			localStreamRef.current = stream;
			if (inputVideoRef.current) inputVideoRef.current.srcObject = stream;

			setStatus("connecting");

			for (
				let attempt = 0;
				attempt <= CONCURRENCY_RETRY_DELAYS_MS.length;
				attempt += 1
			) {
				const outcome = await connectOnce();
				if (!isCurrent() || outcome === "live" || outcome === "error") return;

				const delay = CONCURRENCY_RETRY_DELAYS_MS[attempt];
				if (delay === undefined) {
					setErrorMessage(
						"Concurrent session limit reached. Another realtime session is still active on this account (another tab, or the desktop app). Stop it, then start again.",
					);
					setStatus("error");
					return;
				}

				setErrorMessage(
					`Concurrent session limit reached -- releasing previous sessions, retrying in ${Math.round(
						delay / 1000,
					)}s...`,
				);
				await wait(delay);
				if (!isCurrent()) return;
				setStatus("connecting");
			}
		} catch (err) {
			setErrorMessage(err instanceof Error ? err.message : String(err));
			setStatus("error");
			cleanup();
		}
	}, [cleanup, connectOnce]);

	const stop = useCallback(() => {
		// cleanup() closes every tracked connection, so stopping here also
		// releases any orphaned session from an earlier start.
		cleanup();
		setStatus("idle");
		setExpanded(false);
	}, [cleanup]);

	const applyPrompt = useCallback(() => {
		if (!connectionRef.current || status !== "live") return;
		connectionRef.current.send({
			prompt,
			enable_prompt_expansion: true,
			reference_image_url: referenceImageUrl ?? null,
		});
	}, [prompt, referenceImageUrl, status]);

	const isLive = status === "live";
	const isBusy = status === "requesting-camera" || status === "connecting";

	// ── Render ─────────────────────────────────────────────────────────────────

	return (
		<>
			{/* ── Expanded output overlay ── */}
			{expanded && (
				<div className="fixed inset-0 z-50 bg-black flex items-center justify-center">
					<video
						ref={outputVideoRef}
						autoPlay
						playsInline
						muted
						className="h-full w-full object-contain"
					/>

					{/* Status chip — top left */}
					<div className="absolute top-4 left-4">
						<StatusBadge status={status} mono />
					</div>

					{/* Collapse button — top right */}
					<button
						type="button"
						onClick={() => setExpanded(false)}
						aria-label="Collapse view"
						className="absolute top-4 right-4 flex items-center gap-1.5 border border-white/20 bg-black/60 px-3 py-1.5 text-xs font-medium text-white backdrop-blur hover:bg-white/10 transition-colors">
						<IconCollapse className="h-3.5 w-3.5" />
						Collapse
					</button>

					{/* Escape hint — bottom centre */}
					<p className="absolute bottom-4 left-1/2 -translate-x-1/2 text-[11px] text-white/25 select-none">
						Press Esc to collapse
					</p>
				</div>
			)}

			{/* ── Main panel ── */}
			<div className="w-full mx-auto p-6 space-y-5 bg-black">
				{/* Header */}
				<div className="flex items-center justify-between gap-4 flex-wrap border-b border-white/10 pb-4">
					<div>
						<h1 className="text-xl font-semibold tracking-tight text-white/90">
							Live Editor
						</h1>
						<p className="text-xs text-white/35 mt-0.5">
							Real-time video editing over WebRTC
						</p>
					</div>
					<StatusBadge status={status} />
				</div>

				{/* Video boxes */}
				<div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
					<VideoBox
						label="Input · your camera"
						videoRef={inputVideoRef}
						muted
						placeholder="Camera feed will appear here"
					/>

					{/* Output box — has the Expand button when not in overlay mode */}
					<div className="space-y-2">
						<div className="flex items-center justify-between">
							<p className="text-xs font-medium uppercase tracking-widest text-white/35">
								Output
							</p>
							<button
								type="button"
								onClick={() => setExpanded(true)}
								aria-label="Expand output view"
								className="flex items-center gap-1 border border-white/12 px-2 py-1 text-[11px] font-medium text-white/45 disabled:opacity-25 disabled:cursor-not-allowed hover:border-white/25 hover:text-white/70 transition-colors">
								<IconExpand className="h-3 w-3" />
								Expand view
							</button>
						</div>
						<div className="relative aspect-video w-full overflow-hidden border border-white/10 bg-black">
							{/* In normal mode this video is visible; in expanded mode the
                  video el moves to the overlay above, this shell stays as a
                  placeholder so the grid doesn't collapse. */}
							{!expanded && (
								<video
									ref={outputVideoRef}
									autoPlay
									playsInline
									muted
									className="h-full w-full object-cover"
								/>
							)}
							{(!isLive || expanded) && (
								<div className="absolute inset-0 flex items-center justify-center text-sm text-white/25">
									{expanded
										? "Playing in expanded view"
										: isBusy
											? "Connecting…"
											: ""}
								</div>
							)}
						</div>
					</div>
				</div>

				{/* Prompt */}
				<div className="space-y-2">
					<label
						htmlFor="live-prompt"
						className="block text-xs font-medium uppercase tracking-widest text-white/35">
						Edit instruction
					</label>
					<div className="flex gap-2 flex-col sm:flex-row">
						<textarea
							id="live-prompt"
							value={prompt}
							onChange={(e) => setPrompt(e.target.value)}
							rows={2}
							className="flex-1 resize-none border border-white/12 px-3 py-2 text-sm text-white/80 placeholder:text-white/20 focus:outline-none focus:ring-1 focus:ring-white/25 bg-white/5"
							placeholder="Substitute the character in the video with the person in the reference image."
						/>
						<button
							type="button"
							onClick={applyPrompt}
							disabled={!isLive || isUploadingReference}
							className="border border-white/15 bg-white px-4 py-2 text-sm font-medium text-black disabled:opacity-25 disabled:cursor-not-allowed hover:bg-white/90 transition-colors">
							Apply
						</button>
					</div>
				</div>

				{/* Reference image */}
				<div className="space-y-2">
					<label className="block text-xs font-medium uppercase tracking-widest text-white/35">
						Reference image{" "}
						<span className="normal-case tracking-normal text-white/25">
							(optional — character swap or try-on)
						</span>
					</label>
					<div className="flex items-center gap-3">
						{referenceImagePreview ? (
							<div className="relative h-16 w-16 shrink-0 overflow-hidden border border-white/12">
								{/* eslint-disable-next-line @next/next/no-img-element */}
								<img
									src={referenceImagePreview}
									alt="Reference"
									className="h-full w-full object-cover"
								/>
								{isUploadingReference && (
									<div className="absolute inset-0 flex items-center justify-center bg-black/70 text-[9px] font-medium text-white/70">
										Uploading…
									</div>
								)}
							</div>
						) : (
							<div className="flex h-16 w-16 shrink-0 items-center justify-center border border-dashed border-white/10 text-[10px] text-white/20">
								None
							</div>
						)}

						<div className="flex flex-col gap-1">
							<label className="cursor-pointer border border-white/12 px-3 py-1.5 text-sm font-medium text-white/70 hover:bg-white/5 transition-colors w-fit">
								Choose image
								<input
									type="file"
									accept="image/jpeg,image/png,image/webp"
									onChange={handleReferenceImageChange}
									className="hidden"
								/>
							</label>
							{referenceImagePreview && (
								<button
									type="button"
									onClick={clearReferenceImage}
									className="text-xs text-white/30 hover:text-white/60 transition-colors w-fit">
									Remove
								</button>
							)}
						</div>
					</div>
				</div>

				{/* Session control */}
				<div className="flex items-center gap-3 pt-1 border-t border-white/8">
					{status === "idle" || status === "error" ? (
						<button
							type="button"
							onClick={start}
							disabled={isUploadingReference}
							className="border border-white/15 bg-white px-4 py-2 text-sm font-medium text-black disabled:opacity-25 disabled:cursor-not-allowed hover:bg-white/90 transition-colors">
							Start session
						</button>
					) : (
						<button
							type="button"
							onClick={stop}
							className="border border-white/15 px-4 py-2 text-sm font-medium text-white/70 hover:bg-white/5 transition-colors">
							Stop session
						</button>
					)}
					{errorMessage && (
						<p className="text-sm text-white/50">{errorMessage}</p>
					)}
				</div>
			</div>
		</>
	);
}

// ── StatusBadge ──────────────────────────────────────────────────────────────

function StatusBadge({
	status,
	mono,
}: {
	status: ConnectionStatus;
	mono?: boolean;
}) {
	const dotClass: Record<ConnectionStatus, string> = {
		idle: "bg-white/20",
		"requesting-camera": "bg-white/45",
		connecting: "bg-white/45",
		live: "bg-white",
		error: "bg-white/55",
	};
	const labels: Record<ConnectionStatus, string> = {
		idle: "Idle",
		"requesting-camera": "Requesting camera…",
		connecting: "Connecting…",
		live: "Live",
		error: "Error",
	};

	return (
		<span
			className={`inline-flex items-center gap-2 border px-3 py-1 text-xs font-medium
      ${
			mono
				? "border-white/20 text-white/70"
				: "border-white/10 text-white/45"
		}`}>
			<span
				className={`h-2 w-2 ${dotClass[status]} ${status === "live" ? "animate-pulse" : ""}`}
			/>
			{labels[status]}
		</span>
	);
}

// ── VideoBox ─────────────────────────────────────────────────────────────────

function VideoBox({
	label,
	videoRef,
	muted,
	placeholder,
}: {
	label: string;
	videoRef: React.RefObject<HTMLVideoElement | null>;
	muted?: boolean;
	placeholder: string;
}) {
	const [hasStream, setHasStream] = useState(false);

	return (
		<div className="space-y-2">
			<p className="text-xs font-medium uppercase tracking-widest text-white/35">
				{label}
			</p>
			<div className="relative aspect-video w-full overflow-hidden border border-white/10 bg-black">
				<video
					ref={videoRef}
					autoPlay
					playsInline
					muted={muted}
					onLoadedMetadata={() => setHasStream(true)}
					onEmptied={() => setHasStream(false)}
					className="h-full w-full object-cover"
				/>
				{!hasStream && (
					<div className="absolute inset-0 flex items-center justify-center text-sm text-white/25">
						{placeholder}
					</div>
				)}
			</div>
		</div>
	);
}
