import { Film, LoaderCircle } from "lucide-react";

export function EmptyState({ title, copy, loading = false, action, onAction, icon: CustomIcon }) {
  const Icon = loading ? LoaderCircle : CustomIcon || Film;
  return (
    <div className="empty-state">
      <Icon className={loading ? "spin" : ""} size={28} aria-hidden="true" />
      <strong>{title}</strong>
      {copy && <span>{copy}</span>}
      {action && onAction && (
        <button className="button empty-action" type="button" onClick={onAction}>{action}</button>
      )}
    </div>
  );
}
