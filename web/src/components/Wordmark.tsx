import { Link } from "react-router-dom";

/** Brand lockup. Geometric mark, never an emoji (DESIGN.md section 8). */
export function Wordmark() {
  return (
    <Link className="wordmark" to="/">
      <span className="wordmark__mark" aria-hidden="true">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <path d="M2 9.5C2 5.9 4.9 3 8.5 3H14v3.2A7.3 7.3 0 0 1 6.7 13.5H2V9.5Z" fill="#fff" />
          <circle cx="10.6" cy="6.4" r="1.05" fill="#6366F1" />
        </svg>
      </span>
      Chirp
    </Link>
  );
}
