import { AlertCircle, CheckCircle2, Info } from "lucide-react";

export function Toast({ toast }) {
  if (!toast?.message) return null;
  const Icon = toast.type === "error" ? AlertCircle : toast.type === "success" ? CheckCircle2 : Info;
  return (
    <div className={`toast is-visible is-${toast.type || "info"}`} role="status" aria-live="polite">
      <Icon size={18} aria-hidden="true" />
      <span>{toast.message}</span>
    </div>
  );
}
