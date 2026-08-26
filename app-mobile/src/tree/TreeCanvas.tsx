/** Neural-network lineage graph: hierarchical layout, pulse glow, signal flow along edges. */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Animated,
  PanResponder,
  StyleSheet,
  View,
  useWindowDimensions,
  type GestureResponderEvent,
  type PanResponderGestureState,
} from "react-native";
import Svg, { Circle, Defs, G, Path, RadialGradient, Stop, Text as SvgText } from "react-native-svg";

import type { LineageTreeOut } from "@/api/lineage";
import { useTheme, type Palette } from "@/theme";

import { muli } from "./fonts";
import { layoutLineageGraph, neighborIds, type GraphLayout, type GraphNode } from "./layout";

const NODE_R = 14;
const GHOST_R = 12;
const PARTICLES_PER_EDGE = 2;

export interface TreeCanvasProps {
  tree: LineageTreeOut;
  selectedUserId: string | null;
  onSelectUser: (userId: string | null) => void;
}

function hexWithAlpha(hex: string, alpha: number): string {
  const cleaned = hex.replace("#", "");
  if (cleaned.length !== 6) return hex;
  const r = Number.parseInt(cleaned.slice(0, 2), 16);
  const g = Number.parseInt(cleaned.slice(2, 4), 16);
  const b = Number.parseInt(cleaned.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function useNeuralClock(): number {
  const [t, setT] = useState(0);
  useEffect(() => {
    let frame = 0;
    let start = 0;
    let raf = 0;
    const tick = (now: number) => {
      if (!start) start = now;
      frame += 1;
      // ~30fps updates keep SVG work light while still feeling alive.
      if (frame % 2 === 0) setT((now - start) / 1000);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return t;
}

interface BranchCurve {
  sx: number;
  sy: number;
  c1x: number;
  c1y: number;
  c2x: number;
  c2y: number;
  tx: number;
  ty: number;
}

/** Organic cubic branch: leaves the big downward, bows out, settles into the little. */
function branchCurve(sx: number, sy: number, tx: number, ty: number): BranchCurve {
  const dx = tx - sx;
  const dy = Math.max(ty - sy, 1);
  const side = Math.sign(dx) || (Math.abs(sx) > 4 ? Math.sign(sx) : 1);
  const spread = Math.abs(dx);
  // Wide fan for siblings; tiny S-wobble on near-vertical links so nothing reads as a ruler.
  const fan = Math.min(spread * 0.55, 64) * side;
  const wobble = spread < 12 ? 22 * side : 0;

  return {
    sx,
    sy,
    c1x: sx + dx * 0.05 + fan * 0.45 + wobble,
    c1y: sy + dy * 0.42,
    c2x: tx - dx * 0.12 + fan * 0.7 - wobble * 0.35,
    c2y: ty - dy * 0.32,
    tx,
    ty,
  };
}

function branchPath(c: BranchCurve): string {
  return `M ${c.sx} ${c.sy} C ${c.c1x} ${c.c1y}, ${c.c2x} ${c.c2y}, ${c.tx} ${c.ty}`;
}

function pointOnBranch(c: BranchCurve, t: number): { x: number; y: number } {
  const u = 1 - t;
  const x =
    u * u * u * c.sx + 3 * u * u * t * c.c1x + 3 * u * t * t * c.c2x + t * t * t * c.tx;
  const y =
    u * u * u * c.sy + 3 * u * u * t * c.c1y + 3 * u * t * t * c.c2y + t * t * t * c.ty;
  return { x, y };
}

function GraphEdges({
  layout,
  focus,
  palette,
  t,
}: {
  layout: GraphLayout;
  focus: Set<string> | null;
  palette: Palette;
  t: number;
}) {
  const byId = useMemo(() => new Map(layout.nodes.map((n) => [n.id, n])), [layout.nodes]);
  const dashOffset = -((t * 28) % 20);

  return (
    <>
      {layout.links.map((link) => {
        const source = byId.get(link.sourceId);
        const target = byId.get(link.targetId);
        if (!source || !target) return null;
        const dimmed = focus !== null && !(focus.has(source.id) && focus.has(target.id));
        const color = dimmed ? palette.border : link.familyColor;
        const curve = branchCurve(source.x, source.y, target.x, target.y);
        const d = branchPath(curve);
        return (
          <G key={link.id}>
            <Path
              d={d}
              stroke={hexWithAlpha(link.familyColor, dimmed ? 0.08 : 0.22)}
              strokeWidth={7}
              fill="none"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <Path
              d={d}
              stroke={color}
              strokeWidth={link.confirmed ? 2.1 : 1.5}
              strokeOpacity={dimmed ? 0.25 : 0.92}
              fill="none"
              strokeDasharray={link.confirmed ? undefined : "4 7"}
              strokeDashoffset={link.confirmed ? undefined : dashOffset}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            {!dimmed
              ? Array.from({ length: PARTICLES_PER_EDGE }, (_, i) => {
                  const phase = (t * 0.32 + i / PARTICLES_PER_EDGE + link.id.length * 0.01) % 1;
                  const { x, y } = pointOnBranch(curve, phase);
                  const pulse = 0.45 + 0.55 * Math.sin(t * 4 + i);
                  return (
                    <Circle
                      key={`${link.id}-p${i}`}
                      cx={x}
                      cy={y}
                      r={2.1 + pulse}
                      fill={hexWithAlpha(link.familyColor, 0.55 + pulse * 0.35)}
                    />
                  );
                })
              : null}
          </G>
        );
      })}
    </>
  );
}

function GraphNodeView({
  node,
  focused,
  dimmed,
  selected,
  palette,
  t,
  showLabel,
  onPress,
}: {
  node: GraphNode;
  focused: boolean;
  dimmed: boolean;
  selected: boolean;
  palette: Palette;
  t: number;
  showLabel: boolean;
  onPress: () => void;
}) {
  const x = node.x;
  const y = node.y;
  const r = node.isGhost ? GHOST_R : NODE_R;
  const pulse = 0.5 + 0.5 * Math.sin(t * 2.4 + node.depth * 0.7);
  const glowR = r + 8 + pulse * 6;
  const fill = node.isGhost
    ? palette.surface
    : hexWithAlpha(node.familyColor, selected || focused ? 0.95 : 0.78);
  const gradId = `glow-${node.id}`;

  return (
    <G opacity={dimmed ? 0.18 : 1}>
      <Defs>
        <RadialGradient id={gradId} cx="50%" cy="50%" rx="50%" ry="50%">
          <Stop offset="0%" stopColor={node.familyColor} stopOpacity={0.55 * pulse} />
          <Stop offset="70%" stopColor={node.familyColor} stopOpacity={0.12 * pulse} />
          <Stop offset="100%" stopColor={node.familyColor} stopOpacity={0} />
        </RadialGradient>
      </Defs>
      <Circle cx={x} cy={y} r={glowR + 10} fill={`url(#${gradId})`} />
      <Circle
        cx={x}
        cy={y}
        r={r + 4 + pulse * 2}
        fill={hexWithAlpha(node.familyColor, 0.16 + pulse * 0.1)}
      />
      <Circle cx={x} cy={y} r={r + 12} fill="transparent" onPress={onPress} />
      <Circle
        cx={x}
        cy={y}
        r={r}
        fill={fill}
        stroke={node.familyColor}
        strokeWidth={selected ? 2.5 : 1.5}
        strokeDasharray={node.isGhost ? "3 3" : undefined}
        onPress={onPress}
      />
      <SvgText
        x={x}
        y={y + 1}
        fill={node.isGhost ? palette.inkFaint : palette.onAccent}
        fontSize={9}
        fontFamily={muli.bold}
        fontWeight="700"
        textAnchor="middle"
        alignmentBaseline="middle"
        onPress={onPress}
      >
        {node.initials}
      </SvgText>
      {showLabel ? (
        <SvgText
          x={x}
          y={y + r + 14}
          fill={palette.inkSecondary}
          fontSize={11}
          fontFamily={muli.medium}
          fontWeight="500"
          textAnchor="middle"
          onPress={onPress}
        >
          {node.shortName}
        </SvgText>
      ) : null}
    </G>
  );
}

export function TreeCanvas({ tree, selectedUserId, onSelectUser }: TreeCanvasProps) {
  const palette = useTheme();
  const t = useNeuralClock();
  const { width: viewW, height: viewH } = useWindowDimensions();
  const layout = useMemo(() => layoutLineageGraph(tree), [tree]);
  const focus = useMemo(
    () => (selectedUserId ? neighborIds(tree, selectedUserId) : null),
    [tree, selectedUserId],
  );

  const canvasH = Math.max(400, Math.min(viewH * 0.58, 560));
  const fitScale = Math.min((viewW - 32) / layout.width, canvasH / layout.height, 1.25);

  const pan = useRef(new Animated.ValueXY({ x: 0, y: 0 })).current;
  const scale = useRef(new Animated.Value(fitScale)).current;
  const scaleRef = useRef(fitScale);
  const offset = useRef({ x: 0, y: 0 });
  const pinchStart = useRef(0);
  const lastDistance = useRef<number | null>(null);

  const panResponder = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => false,
      onMoveShouldSetPanResponder: (_e, g) => Math.abs(g.dx) > 4 || Math.abs(g.dy) > 4,
      onPanResponderGrant: (e: GestureResponderEvent) => {
        pan.setOffset({ x: offset.current.x, y: offset.current.y });
        pan.setValue({ x: 0, y: 0 });
        const touches = e.nativeEvent.touches ?? [];
        if (touches.length >= 2) {
          const dx = touches[0].pageX - touches[1].pageX;
          const dy = touches[0].pageY - touches[1].pageY;
          lastDistance.current = Math.hypot(dx, dy);
          pinchStart.current = scaleRef.current;
        } else {
          lastDistance.current = null;
        }
      },
      onPanResponderMove: (e: GestureResponderEvent, g: PanResponderGestureState) => {
        const touches = e.nativeEvent.touches ?? [];
        if (touches.length >= 2 && lastDistance.current) {
          const dx = touches[0].pageX - touches[1].pageX;
          const dy = touches[0].pageY - touches[1].pageY;
          const dist = Math.hypot(dx, dy);
          const next = Math.min(
            2.8,
            Math.max(0.35, pinchStart.current * (dist / lastDistance.current)),
          );
          scaleRef.current = next;
          scale.setValue(next);
          return;
        }
        pan.setValue({ x: g.dx, y: g.dy });
      },
      onPanResponderRelease: (_e, g) => {
        offset.current = { x: offset.current.x + g.dx, y: offset.current.y + g.dy };
        pan.flattenOffset();
        lastDistance.current = null;
      },
      onPanResponderTerminate: (_e, g) => {
        offset.current = { x: offset.current.x + g.dx, y: offset.current.y + g.dy };
        pan.flattenOffset();
        lastDistance.current = null;
      },
    }),
  ).current;

  return (
    <View
      style={[
        styles.frame,
        { height: canvasH, backgroundColor: palette.bg, borderColor: palette.border },
      ]}
      {...panResponder.panHandlers}
      {...({
        onWheel: (e: { deltaY: number; preventDefault: () => void }) => {
          e.preventDefault();
          const factor = e.deltaY > 0 ? 0.92 : 1.08;
          const next = Math.min(2.8, Math.max(0.35, scaleRef.current * factor));
          scaleRef.current = next;
          scale.setValue(next);
        },
      } as object)}
    >
      <Animated.View
        style={[
          styles.world,
          {
            transform: [{ translateX: pan.x }, { translateY: pan.y }, { scale }],
          },
        ]}
      >
        <Svg
          width={layout.width}
          height={layout.height}
          viewBox={`${-layout.width / 2} ${-layout.height / 2} ${layout.width} ${layout.height}`}
        >
          <GraphEdges layout={layout} focus={focus} palette={palette} t={t} />
          {layout.nodes.map((node) => {
            const selected = node.id === selectedUserId;
            const focused = focus?.has(node.id) ?? false;
            const dimmed = focus !== null && !focused;
            return (
              <GraphNodeView
                key={node.id}
                node={node}
                selected={selected}
                focused={focused}
                dimmed={dimmed}
                palette={palette}
                t={t}
                showLabel
                onPress={() => onSelectUser(selected ? null : node.id)}
              />
            );
          })}
        </Svg>
      </Animated.View>
    </View>
  );
}

const styles = StyleSheet.create({
  frame: {
    borderRadius: 16,
    borderWidth: StyleSheet.hairlineWidth,
    overflow: "hidden",
    alignItems: "center",
    justifyContent: "center",
  },
  world: {
    alignItems: "center",
    justifyContent: "center",
  },
});
