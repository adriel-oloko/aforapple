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

	const cleanup = useCallback(() => {
		peerConnectionRef.current?.close();
		peerConnectionRef.current = null;
		connectionRef.current?.close();
		connectionRef.current = null;
		localStreamRef.current?.getTracks().forEach((t) => t.stop());
		localStreamRef.current = null;
		outputStreamRef.current = null;
		if (inputVideoRef.current) inputVideoRef.current.srcObject = null;
		if (outputVideoRef.current) outputVideoRef.current.srcObject = null;
	}, []);

	useEffect(() => () => cleanup(), [cleanup]);

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
					setStatus("live");
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
			}
		} catch (err) {
			setErrorMessage(err instanceof Error ? err.message : String(err));
			setStatus("error");
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

	const start = useCallback(async () => {
		setErrorMessage(null);
		setStatus("requesting-camera");

		try {
			const stream = await navigator.mediaDevices.getUserMedia({
				video: { width: { ideal: 1280 }, height: { ideal: 720 } },
				audio: false,
			});
			localStreamRef.current = stream;
			if (inputVideoRef.current) inputVideoRef.current.srcObject = stream;

			setStatus("connecting");

			const connection = fal.realtime.connect<LiveRealtimeInput>(
				MODEL_ENDPOINT,
				{
					connectionKey: `live-session-${Date.now()}`,
					tokenProvider,
					tokenExpirationSeconds: 120,
					onResult: handleResult,
					onError: (err) => {
						setErrorMessage(
							err instanceof Error ? err.message : String(err),
						);
						setStatus("error");
					},
				},
			);
			connectionRef.current = connection;

			connection.send({
				prompt,
				enable_prompt_expansion: true,
				reference_image_url: referenceImageUrl ?? null,
			});
		} catch (err) {
			setErrorMessage(err instanceof Error ? err.message : String(err));
			setStatus("error");
			cleanup();
		}
	}, [cleanup, handleResult, prompt, referenceImageUrl]);

	const stop = useCallback(() => {
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
							disabled={isBusy}
							className="border border-white/15 px-4 py-2 text-sm font-medium text-white/70 disabled:opacity-25 hover:bg-white/5 transition-colors">
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
