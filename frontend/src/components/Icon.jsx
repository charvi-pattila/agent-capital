// Single stroke-based icon set (24px grid) so the UI has no emoji anywhere.
// Usage: <Icon name="pause" size={16} />. Colour follows the text colour.
const PATHS = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
  bolt: <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />,
  terminal: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m7 9 3 3-3 3" /><path d="M12 15h5" /></>,
  split: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M12 4v16" /></>,
  brain: <><path d="M12 4a4 4 0 0 0-4 4v1a3 3 0 0 0-2 5 3 3 0 0 0 2 5h8a3 3 0 0 0 2-5 3 3 0 0 0-2-5V8a4 4 0 0 0-4-4z" /><path d="M12 4v15" /></>,
  flask: <><path d="M9 3h6" /><path d="M10 3v6L4.5 18a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9V3" /></>,
  eye: <><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></>,
  scale: <><path d="M12 3v18" /><path d="M5 7h14" /><path d="m5 7-3 7a3 3 0 0 0 6 0L5 7z" /><path d="m19 7-3 7a3 3 0 0 0 6 0l-3-7z" /><path d="M8 21h8" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 8h.01" /></>,
  more: <><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></>,
  back: <path d="m15 5-7 7 7 7" />,
  chevron: <path d="m9 6 6 6-6 6" />,
  bell: <><path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4l2-2z" /><path d="M10 20a2 2 0 0 0 4 0" /></>,
  power: <><path d="M12 3v9" /><path d="M6.3 6.3a8 8 0 1 0 11.4 0" /></>,
  pause: <><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></>,
  play: <path d="M7 5v14l11-7z" />,
  scissors: <><circle cx="6" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M20 4 8.1 15.9" /><path d="M14.5 14.5 20 20" /><path d="M8.1 8.1 12 12" /></>,
  keyboard: <><rect x="2" y="6" width="20" height="12" rx="2" /><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M8 14h8" /></>,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  trash: <><path d="M4 7h16" /><path d="M10 11v6M14 11v6" /><path d="M6 7l1 13h10l1-13" /><path d="M9 7V4h6v3" /></>,
  check: <path d="m5 12 5 5 9-10" />,
  x: <><path d="M6 6l12 12" /><path d="M18 6 6 18" /></>,
  warning: <><path d="M12 3 2 20h20L12 3z" /><path d="M12 10v4" /><path d="M12 17h.01" /></>,
  download: <><path d="M12 4v11" /><path d="m7 10 5 5 5-5" /><path d="M4 20h16" /></>,
  folder: <path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6z" />,
  "arrow-up": <><path d="M12 19V5" /><path d="m6 11 6-6 6 6" /></>,
  "arrow-down": <><path d="M12 5v14" /><path d="m6 13 6 6 6-6" /></>,
  enter: <><path d="M20 5v6a2 2 0 0 1-2 2H5" /><path d="m9 9-4 4 4 4" /></>,
  tab: <><path d="M4 12h12" /><path d="m12 8 4 4-4 4" /><path d="M20 6v12" /></>,
  note: <><path d="M6 3h9l5 5v13H6z" /><path d="M14 3v6h6" /><path d="M9 13h6M9 17h6" /></>,
  bug: <><path d="M8 9a4 4 0 0 1 8 0v6a4 4 0 0 1-8 0z" /><path d="M3 12h5M16 12h5M5 6l3 3M19 6l-3 3M5 19l3-3M19 19l-3-3" /></>,
  spark: <><path d="M12 3v4M12 17v4M3 12h4M17 12h4" /><path d="m6.3 6.3 2.8 2.8M14.9 14.9l2.8 2.8M6.3 17.7l2.8-2.8M14.9 9.1l2.8-2.8" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  camera: <><path d="M4 8h3l2-3h6l2 3h3v11H4z" /><circle cx="12" cy="13" r="3" /></>,
  moon: <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />,
  globe: <><circle cx="12" cy="12" r="9" /><path d="M3 12h18" /><path d="M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></>,
  file: <><path d="M6 3h9l5 5v13H6z" /><path d="M14 3v6h6" /></>,
  merge: <><path d="M6 4v6a4 4 0 0 0 4 4h8" /><path d="m14 10 4 4-4 4" /><path d="M6 14v6" /></>,
  logo: <><path d="M4 20V9l8-5 8 5v11" /><path d="M9 20v-6h6v6" /><path d="M2 20h20" /></>,
};

export default function Icon({ name, size = 18, strokeWidth = 1.8, className = "", style }) {
  const body = PATHS[name];
  if (!body) return null;
  return (
    <svg
      className={"icon " + className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      {body}
    </svg>
  );
}

// Replaces the per-project emoji: a rounded tile with the project's initials.
// Deterministic hue per name so two projects side by side are told apart.
export function ProjectMark({ name, size = 36 }) {
  const words = (name || "?").trim().split(/[\s_-]+/).filter(Boolean);
  const initials = (words.length >= 2 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2)).toUpperCase();
  let h = 0;
  for (const ch of name || "") h = (h * 31 + ch.charCodeAt(0)) % 360;
  const hue = 320 + ((h % 60) - 30); // stays in the pink/plum family
  return (
    <span
      className="project-mark"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.38),
        background: `hsl(${hue} 45% 22%)`,
        color: `hsl(${hue} 80% 82%)`,
      }}
    >
      {initials}
    </span>
  );
}
