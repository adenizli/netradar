export interface Point {
  id: string;
  x: number;
  y: number;
}
export interface Cluster {
  id: string;
  x: number;
  y: number;
  members: Point[];
}
export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}
const intersects = (a: Rect, b: Rect) =>
  a.x < b.x + b.width &&
  a.x + a.width > b.x &&
  a.y < b.y + b.height &&
  a.y + a.height > b.y;
/** Spatial grid keeps marker hit targets separate without moving aircraft coordinates. */
export function clusterPoints(points: Point[], radius = 36): Cluster[] {
  const grid = new Map<string, Cluster[]>(),
    clusters: Cluster[] = [];
  for (const point of [...points].sort((a, b) => a.id.localeCompare(b.id))) {
    const gx = Math.floor(point.x / radius),
      gy = Math.floor(point.y / radius);
    let match: Cluster | undefined;
    for (let dx = -1; dx <= 1 && !match; dx++)
      for (let dy = -1; dy <= 1 && !match; dy++)
        match = grid
          .get(`${gx + dx}:${gy + dy}`)
          ?.find((c) => Math.hypot(c.x - point.x, c.y - point.y) < radius);
    if (match) match.members.push(point);
    else {
      const cluster = { ...point, members: [point] };
      clusters.push(cluster);
      const key = `${gx}:${gy}`;
      grid.set(key, [...(grid.get(key) ?? []), cluster]);
    }
  }
  return clusters;
}
export function placeLabels(
  points: Point[],
  width: number,
  height: number,
  selected: string | null,
  reserved: Rect[] = [],
): Map<string, Rect> {
  const occupied = [
    ...reserved,
    ...points.map((p) => ({ x: p.x - 19, y: p.y - 19, width: 38, height: 38 })),
  ];
  const labels = new Map<string, Rect>();
  for (const p of [...points].sort(
    (a, b) =>
      Number(b.id === selected) - Number(a.id === selected) ||
      a.id.localeCompare(b.id),
  )) {
    const options = [
      [22, -21],
      [-150, -21],
      [22, 10],
      [-150, 10],
      [-63, -69],
      [-63, 24],
    ];
    for (const [dx, dy] of options) {
      const rect = { x: p.x + dx, y: p.y + dy, width: 128, height: 41 };
      if (
        rect.x < 12 ||
        rect.y < 12 ||
        rect.x + rect.width > width - 12 ||
        rect.y + rect.height > height - 12 ||
        occupied.some((r) => intersects(rect, r))
      )
        continue;
      labels.set(p.id, rect);
      occupied.push({
        ...rect,
        x: rect.x - 4,
        y: rect.y - 4,
        width: rect.width + 8,
        height: rect.height + 8,
      });
      break;
    }
  }
  return labels;
}
