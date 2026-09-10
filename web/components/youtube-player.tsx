"use client";

import { useEffect, useRef, useState } from "react";
import { Volume2, VolumeX } from "lucide-react";
import type { Snapshot, YouTubePlayer } from "@/lib/types";
import { authoritativePosition, shouldSeek } from "@/lib/timeline";

export default function YouTubePlayerView({ playback }: { playback: Snapshot["playback"] }) {
  const host = useRef<HTMLDivElement>(null); const player = useRef<YouTubePlayer | null>(null);
  const [consented, setConsented] = useState(false); const [muted, setMuted] = useState(true); const [volume, setVolume] = useState(55);
  const videoId = playback.track?.youtube_id;
  useEffect(() => {
    if (!videoId || !host.current) return;
    let disposed = false;
    const create = () => {
      if (disposed || !host.current || !window.YT) return;
      player.current?.destroy();
      player.current = new window.YT.Player(host.current, { width: "100%", height: "100%", videoId, playerVars: { playsinline: 1, controls: 1, rel: 0 }, events: { onReady: () => { player.current?.mute(); player.current?.seekTo(authoritativePosition(playback), true); } } });
    };
    if (window.YT) create(); else {
      const existing = document.querySelector("script[data-youtube-api]");
      if (!existing) { const script = document.createElement("script"); script.src = "https://www.youtube.com/iframe_api"; script.dataset.youtubeApi = "true"; document.head.appendChild(script); }
      const prior = window.onYouTubeIframeAPIReady; window.onYouTubeIframeAPIReady = () => { prior?.(); create(); };
    }
    return () => { disposed = true; player.current?.destroy(); player.current = null; };
  }, [videoId]);
  useEffect(() => {
    const interval = window.setInterval(() => {
      if (!player.current || !consented) return;
      const target = authoritativePosition(playback);
      if (shouldSeek(player.current.getCurrentTime(), target)) player.current.seekTo(target, true);
      if (playback.status === "playing") player.current.playVideo(); else player.current.pauseVideo();
    }, 3000);
    return () => window.clearInterval(interval);
  }, [playback, consented]);
  if (!playback.track) return <div className="grid h-full min-h-[220px] place-items-center p-8 text-center text-white/55">The TV will show the authoritative room track.</div>;
  if (!videoId) return <div className="grid h-full min-h-[220px] place-items-center p-8 text-center"><div><p className="font-semibold">Preview unavailable</p><a className="mt-2 block break-all text-sm text-violet-300 underline" href={playback.track.source_url} target="_blank" rel="noreferrer">Open the supported source</a><p className="mt-2 text-xs text-white/50">Parlay does not simulate playback for unsupported sources.</p></div></div>;
  return <div className="relative h-full min-h-[220px] overflow-hidden bg-black">
    <div ref={host} className="absolute inset-0 min-h-[220px] min-w-[200px]" />
    {!consented && <button className="absolute inset-0 z-10 grid w-full place-items-center bg-black/65 p-8 text-center" onClick={() => { setConsented(true); player.current?.playVideo(); }}><span><strong className="block text-lg">Tap to play on this device</strong><span className="mt-2 block text-sm text-white/70">The visible YouTube player follows the room timeline.</span></span></button>}
    <div className="absolute bottom-12 right-2 z-20 flex items-center gap-2 rounded-full bg-black/75 px-3 py-2">
      <button aria-label={muted ? "Unmute local player" : "Mute local player"} onClick={() => { const next = !muted; setMuted(next); next ? player.current?.mute() : player.current?.unMute(); }}>{muted ? <VolumeX size={18} /> : <Volume2 size={18} />}</button>
      <input aria-label="Local volume" className="range w-20" type="range" min="0" max="100" value={volume} onChange={e => { const next = Number(e.target.value); setVolume(next); player.current?.setVolume(next); }} />
    </div>
  </div>;
}
