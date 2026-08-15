/**
 * The hero's product shot.
 *
 * Hand-built from the same tokens the app renders with, rather than a captured
 * screenshot: there are no image assets in this repo, and a grey placeholder
 * box is exactly the slop DESIGN.md section 10 bans. Pixel-accurate screenshots
 * of the running app would be strictly better and are a fast-follow once
 * someone can capture them — that needs a person and a simulator, not a build.
 *
 * The whole thing is aria-hidden behind a single descriptive label on the
 * wrapper: a screen reader gains nothing from walking a decorative fake feed
 * post by post.
 */
export function PhoneMock() {
  return (
    <div
      className="phone"
      role="img"
      aria-label="The Chirp home feed on a phone, showing campus posts from students."
    >
      <div className="phone__screen" aria-hidden="true">
        <div>
          <p className="phone__eyebrow">Home</p>
          <p className="phone__title">Your campus, right now</p>
        </div>

        <div className="phone__pills">
          <span className="phone__pill phone__pill--on">For You</span>
          <span className="phone__pill">Campus</span>
        </div>

        <article className="post">
          <div className="post__head">
            <div className="avatar">MR</div>
            <div className="post__who">
              <span className="post__name">Maria Reyes</span>
              <span className="post__meta">Campus &middot; 12m</span>
            </div>
          </div>
          <div className="post__media" />
          <p className="post__body">Intramural finals moved to the turf field. Bring a jacket.</p>
          <div className="post__actions">
            {/* The one warm moment on this surface (DESIGN.md 10.4). */}
            <span className="act act--warm">42</span>
            <span className="act">9</span>
            <span className="act">Share</span>
          </div>
        </article>

        <article className="post">
          <div className="post__head">
            <div className="avatar">TJ</div>
            <div className="post__who">
              <span className="post__name">Tyler Jordan</span>
              <span className="post__meta">Campus &middot; 1h</span>
            </div>
          </div>
          <p className="post__body">
            Anyone else&apos;s chapter still collecting dues by Venmo screenshot
          </p>
          <div className="post__actions">
            <span className="act act--on">128</span>
            <span className="act">31</span>
          </div>
        </article>
      </div>
    </div>
  );
}
