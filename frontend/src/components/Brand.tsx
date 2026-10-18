export function RadarGlyph({ size = 30 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 36 36"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="18" cy="18" r="14" stroke="currentColor" strokeWidth="1.4" />
      <circle
        cx="18"
        cy="18"
        r="8"
        stroke="currentColor"
        strokeWidth="1.2"
        opacity=".5"
      />
      <path
        d="M18 18 28 8M18 1v4M1 18h4M18 31v4M31 18h4"
        stroke="currentColor"
        strokeWidth="1.4"
      />
      <circle cx="18" cy="18" r="2.5" fill="currentColor" />
      <circle cx="11" cy="12" r="1.5" fill="currentColor" />
    </svg>
  );
}
