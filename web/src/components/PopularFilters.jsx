import { useState } from "react";
import { Cloud, EyeOff, SlidersHorizontal, X } from "lucide-react";
import { AGE_OPTIONS, DURATION_OPTIONS } from "../lib/popularity";

export function PopularFilters({ filters, tags, loading, onChange, onTopic }) {
  const [tagQuery, setTagQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [open, setOpen] = useState(() => !window.matchMedia("(max-width: 760px)").matches);
  const selected = [
    ...filters.topics,
    filters.duration && DURATION_OPTIONS.find(([id]) => id === filters.duration)?.[1],
    filters.age && AGE_OPTIONS.find(([id]) => id === filters.age)?.[1],
    filters.unwatched && "隐藏看过", filters.cloudOnly && "只看已存云盘",
    filters.exclude && `排除：${filters.exclude}`,
  ].filter(Boolean);
  const matching = tags.filter(([tag]) => tag.toLowerCase().includes(tagQuery.trim().toLowerCase()));
  const visible = expanded || tagQuery ? matching : matching.slice(0, 12);
  return (
    <section className="popular-filters" aria-label="热门偏好筛选">
      <div className="popular-filter-title">
        <SlidersHorizontal size={15} aria-hidden="true" /><strong>按你的口味挑选</strong>
        <button type="button" className="button is-quiet" aria-expanded={open} aria-controls="popular-filter-fields" onClick={() => setOpen(!open)}>{open ? "收起筛选" : "展开筛选"}{!open && selected.length > 0 ? `（${selected.length}）` : ""}</button>
      </div>
      {!open && <p className="popular-filter-summary">{selected.length ? selected.join(" · ") : "选标签、时长和日期，隐藏看过或排除不喜欢的内容"}</p>}
      <div id="popular-filter-fields" hidden={!open}>
      <div className="popular-controls">
        <label className="select-wrap"><span className="select-label">时长</span>
          <select aria-label="影片时长" value={filters.duration} onChange={(event) => onChange({ duration: event.target.value })}>
            {DURATION_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label className="select-wrap"><span className="select-label">源站日期</span>
          <select aria-label="源站日期" value={filters.age} onChange={(event) => onChange({ age: event.target.value })}>
            {AGE_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <button type="button" className={`chip is-toggle${filters.unwatched ? " is-active" : ""}`} aria-pressed={filters.unwatched} onClick={() => onChange({ unwatched: !filters.unwatched })}>
          <EyeOff size={14} aria-hidden="true" />隐藏看过
        </button>
        <button type="button" className={`chip is-toggle${filters.cloudOnly ? " is-active" : ""}`} aria-pressed={filters.cloudOnly} onClick={() => onChange({ cloudOnly: !filters.cloudOnly })}>
          <Cloud size={14} aria-hidden="true" />只看已存云盘
        </button>
      </div>
      <div className="popular-tag-heading">
        <strong>偏好标签</strong><span>多选需同时包含</span>
        <label className="popular-tag-search"><span className="sr-only">查找偏好标签</span>
          <input type="search" value={tagQuery} onChange={(event) => setTagQuery(event.target.value)} placeholder="查找标签" />
        </label>
      </div>
      <div className="popular-tags" role="group" aria-label="偏好标签">
        <button className={`chip is-small${!filters.topics.length ? " is-active" : ""}`} type="button" aria-pressed={!filters.topics.length} onClick={() => onChange({ topics: [] })}>不限标签</button>
        {filters.topics.filter((tag) => !visible.some(([name]) => name === tag)).map((tag) => (
          <button className="chip is-small is-active" type="button" aria-pressed key={tag} onClick={() => onTopic(tag)}>{tag}<X size={12} aria-hidden="true" /></button>
        ))}
        {visible.map(([tag, count]) => (
          <button className={`chip is-small${filters.topics.includes(tag) ? " is-active" : ""}`} type="button" key={tag} aria-pressed={filters.topics.includes(tag)} onClick={() => onTopic(tag)}>
            {tag}<small>{count}</small>
          </button>
        ))}
        {!tagQuery && matching.length > 12 && <button className="chip is-small is-more" type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "收起标签" : `更多标签（${matching.length - 12}）`}</button>}
        {!matching.length && <span className="row-hint">{loading ? "正在载入标签…" : tagQuery ? "没有这个标签，可用顶部关键词搜索" : "当前范围没有标签信息"}</span>}
      </div>
      <label className="popular-exclude"><span>排除不想看的</span>
        <input type="search" aria-label="排除关键词" value={filters.exclude} onChange={(event) => onChange({ exclude: event.target.value }, false)} placeholder="输入关键词，多个词用空格分隔" />
      </label>
      <p className="popular-filter-help">标签数量为所选站点的全量收录数；结果按全部条件筛选。指定时长或日期时仅包含信息完整的影片。观看记录来自当前浏览器。</p>
      </div>
    </section>
  );
}
