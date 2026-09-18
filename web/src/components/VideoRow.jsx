import { ArrowRight, ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { videoKey } from "../lib/format";

// A horizontally scrolling shelf of cards with a heading and an optional
// "see all" action. Arrows are hidden on touch layouts where swiping is natural.
export function VideoRow({
  kicker, title, description, count, items = [], renderItem,
  actionLabel = "查看全部", actionHref, onAction,
}) {
  const scroller = useRef(null);
  const [scrollable, setScrollable] = useState(false);

  useEffect(() => {
    const node = scroller.current;
    if (!node) return undefined;
    const check = () => setScrollable(node.scrollWidth > node.clientWidth + 4);
    check();
    const observer = new ResizeObserver(check);
    observer.observe(node);
    return () => observer.disconnect();
  }, [items.length]);

  const scroll = (direction) => {
    const node = scroller.current;
    if (!node) return;
    node.scrollBy({ left: direction * Math.max(240, node.clientWidth * 0.85), behavior: "smooth" });
  };

  return (
    <section className="video-row" aria-label={title}>
      <div className="row-head">
        <div className="row-title">
          {kicker && <span className="row-kicker">{kicker}</span>}
          <h2>
            {title}
            {count !== undefined && count !== null && <small>{Number(count).toLocaleString()}</small>}
          </h2>
          {description && <p>{description}</p>}
        </div>
        <div className="row-tools">
          {actionHref && (
            <a className="row-link" href={actionHref}>{actionLabel}<ArrowRight size={14} aria-hidden="true" /></a>
          )}
          {!actionHref && onAction && (
            <button className="row-link" type="button" onClick={onAction}>{actionLabel}<ArrowRight size={14} aria-hidden="true" /></button>
          )}
          {scrollable && (
            <>
              <button className="row-arrow" type="button" aria-label="向左滚动" onClick={() => scroll(-1)}><ChevronLeft size={16} /></button>
              <button className="row-arrow" type="button" aria-label="向右滚动" onClick={() => scroll(1)}><ChevronRight size={16} /></button>
            </>
          )}
        </div>
      </div>
      <div className="row-scroller" ref={scroller}>
        {items.map((video) => (
          <div className="row-item" key={videoKey(video)}>{renderItem(video)}</div>
        ))}
      </div>
    </section>
  );
}
