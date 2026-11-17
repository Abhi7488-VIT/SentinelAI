/** SentinelAI's mark: a watch ring with the signal it found, in the data accent. */
export function ProductMark({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="10" r="7.25" fill="none" stroke="var(--accent-data)" strokeWidth="2.5" />
      <circle cx="13.5" cy="6.5" r="2.5" fill="var(--text)" />
    </svg>
  );
}

export function ProductName({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 font-semibold text-foreground ${className}`}>
      <ProductMark />
      SentinelAI
    </span>
  );
}
