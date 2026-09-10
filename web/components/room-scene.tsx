"use client";

import { Canvas, ThreeEvent, useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import type { Member, Presence } from "@/lib/types";

const palettes: Record<string, [string, string, string]> = { midnight: ["#11152a", "#727cff", "#d7d9ff"], sunset: ["#2c1225", "#ff7a78", "#ffd2a1"], forest: ["#10251d", "#57c785", "#d6f5df"] };
const avatarShape = (avatar?: string) => avatar === "round" ? 0 : avatar === "tall" ? 1 : 2;

function Human({ member, target, own, reduced }: { member: Member; target: Presence; own: boolean; reduced: boolean }) {
  const group = useRef<THREE.Group>(null);
  const color = member.outfit === "coral" ? "#fb7185" : member.outfit === "mint" ? "#5eead4" : member.outfit === "gold" ? "#fbbf24" : "#a78bfa";
  useFrame((_, delta) => {
    if (!group.current) return;
    const amount = reduced ? 1 : Math.min(1, delta * 9);
    group.current.position.x = THREE.MathUtils.lerp(group.current.position.x, target.x, amount);
    group.current.position.z = THREE.MathUtils.lerp(group.current.position.z, target.z, amount);
    group.current.rotation.y = THREE.MathUtils.lerp(group.current.rotation.y, target.rotation, amount);
  });
  return <group ref={group} position={[target.x, 0, target.z]}>
    <mesh position={[0, 1.15, 0]} castShadow><capsuleGeometry args={[.27, avatarShape(member.avatar) ? .55 : .42, 5, 10]} /><meshStandardMaterial color={color} /></mesh>
    <mesh position={[0, 1.82, 0]} castShadow><sphereGeometry args={[avatarShape(member.avatar) === 2 ? .34 : .3, 16, 12]} /><meshStandardMaterial color="#d9a47f" /></mesh>
    <mesh position={[0, 1.86, .27]}><sphereGeometry args={[.035, 8, 8]} /><meshBasicMaterial color="#15131b" /></mesh>
    <mesh position={[0, .55, 0]} castShadow><capsuleGeometry args={[.11, .58, 4, 8]} /><meshStandardMaterial color="#252a3a" /></mesh>
    <mesh position={[0, .12, 0]} rotation={[Math.PI / 2, 0, 0]}><ringGeometry args={[.4, .48, 24]} /><meshBasicMaterial color={own ? "#ffffff" : color} transparent opacity={.75} /></mesh>
  </group>;
}

function World({ members, ownId, presence, theme, reduced, onMove }: { members: Member[]; ownId: string; presence: Presence[]; theme: string; reduced: boolean; onMove(position: Presence): void }) {
  const [own, setOwn] = useState<Presence>({ user_id: ownId, x: 0, z: 2.2, rotation: Math.PI });
  const keys = useRef(new Set<string>());
  const palette = palettes[theme] || palettes.midnight;
  const positions = useMemo(() => new Map(presence.map(p => [String(p.user_id), p])), [presence]);
  useEffect(() => {
    const down = (event: KeyboardEvent) => keys.current.add(event.key.toLowerCase());
    const up = (event: KeyboardEvent) => keys.current.delete(event.key.toLowerCase());
    window.addEventListener("keydown", down); window.addEventListener("keyup", up);
    return () => { window.removeEventListener("keydown", down); window.removeEventListener("keyup", up); };
  }, []);
  useFrame((_, delta) => {
    const dx = Number(keys.current.has("d") || keys.current.has("arrowright")) - Number(keys.current.has("a") || keys.current.has("arrowleft"));
    const dz = Number(keys.current.has("s") || keys.current.has("arrowdown")) - Number(keys.current.has("w") || keys.current.has("arrowup"));
    if (!dx && !dz) return;
    setOwn(current => {
      const length = Math.hypot(dx, dz) || 1; const speed = Math.min(delta, .05) * 3;
      const next = { ...current, x: THREE.MathUtils.clamp(current.x + dx / length * speed, -4.8, 4.8), z: THREE.MathUtils.clamp(current.z + dz / length * speed, -2.8, 3.8), rotation: Math.atan2(dx, dz) };
      onMove(next); return next;
    });
  });
  const tapFloor = (event: ThreeEvent<PointerEvent>) => {
    event.stopPropagation();
    const next = { ...own, x: THREE.MathUtils.clamp(event.point.x, -4.8, 4.8), z: THREE.MathUtils.clamp(event.point.z, -2.8, 3.8), rotation: Math.atan2(event.point.x - own.x, event.point.z - own.z) };
    setOwn(next); onMove(next);
  };
  return <>
    <color attach="background" args={[palette[0]]} /><fog attach="fog" args={[palette[0], 8, 17]} />
    <ambientLight intensity={1.4} /><directionalLight position={[3, 7, 4]} intensity={2} castShadow={!reduced} />
    <mesh rotation={[-Math.PI / 2, 0, 0]} onPointerDown={tapFloor} receiveShadow><planeGeometry args={[12, 8]} /><meshStandardMaterial color={palette[0]} roughness={.8} /></mesh>
    <mesh position={[0, 2.3, -3.7]}><boxGeometry args={[5.3, 3, .15]} /><meshStandardMaterial color="#08090d" emissive={palette[1]} emissiveIntensity={.08} /></mesh>
    <mesh position={[0, .18, -1.5]}><boxGeometry args={[3.7, .35, .65]} /><meshStandardMaterial color={palette[2]} roughness={.5} /></mesh>
    {members.map((member, index) => {
      const fallback = { user_id: String(member.user_id), x: (index - (members.length - 1) / 2) * 1.2, z: 1.3 + (index % 2) * .8, rotation: Math.PI };
      const target = String(member.user_id) === String(ownId) ? own : positions.get(String(member.user_id)) || fallback;
      return <Human key={member.user_id} member={member} target={target} own={String(member.user_id) === String(ownId)} reduced={reduced} />;
    })}
  </>;
}

export default function RoomScene(props: { members: Member[]; ownId: string; presence: Presence[]; theme: string; reduced: boolean; onMove(position: Presence): void }) {
  const [webgl, setWebgl] = useState(true);
  useEffect(() => { try { const canvas = document.createElement("canvas"); setWebgl(Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"))); } catch { setWebgl(false); } }, []);
  if (!webgl) return <div className="grid h-full place-items-center p-8 text-center"><div><p className="font-semibold">Lightweight room view</p><p className="mt-2 text-sm text-white/60">3D is unavailable on this device. Room and playback controls still work.</p></div></div>;
  return <Canvas shadows={!props.reduced} dpr={props.reduced ? 1 : [1, 1.5]} camera={{ position: [0, 6.5, 8], fov: 48 }} gl={{ powerPreference: "high-performance", antialias: !props.reduced }}>
    <World {...props} />
  </Canvas>;
}
