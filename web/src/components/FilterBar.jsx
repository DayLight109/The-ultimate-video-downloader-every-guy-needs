import { ListFilter, Sparkles, Tag, X } from "lucide-react";
import { useState } from "react";
import { siteName } from "../lib/format";

const TAG_PREVIEW = 14;
const TAG_MAX = 80;

export function FilterBar({
  metadata = [], site = "", tag = "", tags = [], tagsReady = false, showTags = false,
  sort = "default", sortOptions = [], recentCount = 0, recentOnly = false, hasFilters = false,
  onSite, onTag, onSort, onRecentToggle, onClear,
}) {
  const [expanded, setExpanded] = useState(false);
  const visibleTags = expanded ? tags.slice(0, TAG_MAX) : tags.slice(0, TAG_PREVIEW);
  const selectedHidden = Boolean(tag) && !visibleTags.some(([value]) => value === tag);

  return (
    <section className="filter-bar" aria-label="影片筛选">
      <div className="filter-row">
        {metadata.length > 0 && (
          <div className="chip-scroller" role="group" aria-label="站点">
            <button className={`chip${!site ? " is-active" : ""}`} type="button" aria-pressed={!site} onClick={() => onSite("")}>
              全部站点
            </button>
            {metadata.map((entry) => (
              <button
                className={`chip${site === entry.site ? " is-active" : ""}`}
                type="button"
                key={entry.site}
                aria-pressed={site === entry.site}
                onClick={() => onSite(entry.site)}
              >
                {siteName(entry.site)}<small>{Number(entry.count || 0).toLocaleString()}</small>
              </button>
            ))}
          </div>
        )}
        <div className="filter-tools">
          {onRecentToggle && recentCount > 0 && (
            <button
              className={`chip is-toggle${recentOnly ? " is-active" : ""}`}
              type="button"
              aria-pressed={recentOnly}
              title="只显示最近 7 天新抓取的影片"
              onClick={onRecentToggle}
            >
              <Sparkles size={13} aria-hidden="true" />仅看新增<small>{recentCount.toLocaleString()}</small>
            </button>
          )}
          <label className="select-wrap sort-select">
            <ListFilter size={15} aria-hidden="true" />
            <span className="select-label">排序</span>
            <select value={sort} onChange={(event) => onSort(event.target.value)} aria-label="排序方式">
              {sortOptions.map((option) => <option value={option.id} key={option.id}>{option.label}</option>)}
            </select>
          </label>
          {hasFilters && (
            <button className="button is-quiet clear-filters" type="button" onClick={onClear}>
              <X size={14} aria-hidden="true" /><span>清除筛选</span>
            </button>
          )}
        </div>
      </div>

      {showTags && (
        <div className="filter-row tag-row">
          <span className="row-label"><Tag size={13} aria-hidden="true" />{site ? `${siteName(site)} 标签` : "常用标签"}</span>
          {!tagsReady ? (
            <span className="row-hint">目录载入完成后可按标签筛选</span>
          ) : !tags.length ? (
            <span className="row-hint">{site ? "该站点没有标签信息" : "暂无标签"}</span>
          ) : (
            <div className={`chip-scroller${expanded ? " is-wrapped" : ""}`} role="group" aria-label="标签">
              <button className={`chip is-small${!tag ? " is-active" : ""}`} type="button" aria-pressed={!tag} onClick={() => onTag("")}>全部</button>
              {selectedHidden && (
                <button className="chip is-small is-active" type="button" aria-pressed onClick={() => onTag("")}>{tag}<X size={11} aria-hidden="true" /></button>
              )}
              {visibleTags.map(([value, count]) => (
                <button
                  className={`chip is-small${tag === value ? " is-active" : ""}`}
                  type="button"
                  key={value}
                  aria-pressed={tag === value}
                  onClick={() => onTag(tag === value ? "" : value)}
                >
                  {value}<small>{Number(count).toLocaleString()}</small>
                </button>
              ))}
              {tags.length > TAG_PREVIEW && (
                <button className="chip is-small is-more" type="button" onClick={() => setExpanded((current) => !current)}>
                  {expanded ? "收起" : `更多 ${Math.min(tags.length, TAG_MAX) - TAG_PREVIEW} 个`}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
