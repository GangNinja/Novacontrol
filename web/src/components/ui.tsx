import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import {
  AlertCircle,
  ArrowDownRight,
  ArrowUpRight,
  Check,
  ChevronDown,
  Info,
  LoaderCircle,
  Search,
  X,
} from "lucide-react";
import { money, statusTone, title } from "../lib";

export function cx(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

/* ------------------------------------------------------------------ toasts */

type ToastTone = "success" | "error" | "info";
type Toast = { id: number; message: string; tone: ToastTone };
type ToastApi = { push: (message: string, tone?: ToastTone) => void };
const ToastContext = createContext<ToastApi>({ push: () => undefined });

let toastSequence = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((message: string, tone: ToastTone = "info") => {
    const id = ++toastSequence;
    setToasts((current) => [...current.slice(-3), { id, message, tone }]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((item) => item.id !== id));
    }, 4800);
  }, []);
  const api = useMemo(() => ({ push }), [push]);
  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <button
            className={cx("toast", `toast-${toast.tone}`)}
            key={toast.id}
            onClick={() => setToasts((current) => current.filter((item) => item.id !== toast.id))}
            type="button"
          >
            {toast.tone === "success" ? <Check size={16} /> : toast.tone === "error" ? <AlertCircle size={16} /> : <Info size={16} />}
            <span>{toast.message}</span>
            <X size={14} className="toast-close" />
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  return useContext(ToastContext);
}

/* ----------------------------------------------------------------- buttons */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger" | "quiet";
  size?: "sm" | "md";
  icon?: ReactNode;
  loading?: boolean;
};

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  loading,
  children,
  className,
  disabled,
  ...props
}: ButtonProps) {
  return (
    <button
      className={cx("button", `button-${variant}`, size === "sm" && "button-sm", className)}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <LoaderCircle size={15} className="spin" /> : icon}
      {children}
    </button>
  );
}

export function IconButton({
  label,
  children,
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button className={cx("icon-button", className)} aria-label={label} title={label} {...props}>
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------- cards */

export function Card({
  children,
  className,
  as: Tag = "section",
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "div" | "article";
}) {
  return <Tag className={cx("card", className)}>{children}</Tag>;
}

export function CardHeader({
  title: heading,
  subtitle,
  action,
  eyebrow,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <div className="card-header">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h2 className="card-title">{heading}</h2>
        {subtitle && <p className="card-subtitle">{subtitle}</p>}
      </div>
      {action && <div className="card-action">{action}</div>}
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title: heading,
  description,
  actions,
  meta,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  meta?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="page-heading">
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{heading}</h1>
        {description && <p>{description}</p>}
        {meta && <div className="page-meta">{meta}</div>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function SectionTitle({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="section-title">
      <h2>{children}</h2>
      {action}
    </div>
  );
}

/* ------------------------------------------------------------------ badges */

export function Badge({
  children,
  tone = "neutral",
  dot = false,
  className,
}: {
  children: ReactNode;
  tone?: "good" | "warn" | "bad" | "info" | "neutral" | "accent";
  dot?: boolean;
  className?: string;
}) {
  return (
    <span className={cx("badge", `badge-${tone}`, className)}>
      {dot && <span className="badge-dot" />}
      {children}
    </span>
  );
}

export function StatusBadge({ value }: { value: unknown }) {
  return (
    <Badge tone={statusTone(value)} dot>
      {title(value || "unknown")}
    </Badge>
  );
}

export function Chip({ children, onClick, active }: { children: ReactNode; onClick?: () => void; active?: boolean }) {
  return (
    <button type="button" className={cx("chip", active && "chip-active")} onClick={onClick}>
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ states */

export function LoadingBlock({ rows = 3, label = "Reading NovaMarket…" }: { rows?: number; label?: string }) {
  return (
    <div className="loading-block" aria-busy="true" aria-label={label}>
      <div className="loading-label">
        <LoaderCircle size={15} className="spin" />
        {label}
      </div>
      {Array.from({ length: rows }).map((_, index) => (
        <div className="skeleton" key={index} style={{ width: `${94 - index * 9}%` }} />
      ))}
    </div>
  );
}

export function EmptyState({
  icon,
  title: heading,
  description,
  action,
  level = 3,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
  /** Heading level; 2 when the empty state is a page section rather than card content. */
  level?: 2 | 3;
}) {
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <div className="empty-state">
      {icon && <div className="empty-icon">{icon}</div>}
      <Heading>{heading}</Heading>
      {description && <p>{description}</p>}
      {action}
    </div>
  );
}

export function ErrorBlock({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-block" role="alert">
      <AlertCircle size={18} />
      <div>
        <strong>Could not load this surface</strong>
        <p>{message}</p>
      </div>
      {onRetry && (
        <Button size="sm" variant="secondary" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function Banner({
  tone = "info",
  title: heading,
  children,
  action,
}: {
  tone?: "info" | "warn" | "bad" | "good";
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className={cx("banner", `banner-${tone}`)}>
      <div className="banner-mark">
        {tone === "bad" ? <AlertCircle size={17} /> : tone === "good" ? <Check size={17} /> : <Info size={17} />}
      </div>
      <div className="banner-copy">
        <strong>{heading}</strong>
        {children && <p>{children}</p>}
      </div>
      {action && <div className="banner-action">{action}</div>}
    </div>
  );
}

/* --------------------------------------------------------------- statistics */

export function Metric({
  label,
  value,
  change,
  help,
  icon,
  tone = "neutral",
  loading,
}: {
  label: string;
  value: ReactNode;
  change?: ReactNode;
  help?: ReactNode;
  icon?: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad" | "accent";
  loading?: boolean;
}) {
  return (
    <div className={cx("metric", `metric-${tone}`)}>
      <div className="metric-top">
        <span>{label}</span>
        {icon && <span className="metric-icon">{icon}</span>}
      </div>
      <div className={cx("metric-value", loading && "metric-loading")}>{loading ? <span className="skeleton skeleton-value" /> : value}</div>
      {(change || help) && (
        <div className="metric-foot">
          {change && <span className={cx("metric-change", typeof value === "string" && value.startsWith("-") && "negative")}>{change}</span>}
          {help && <span className="metric-help">{help}</span>}
        </div>
      )}
    </div>
  );
}

export function KeyValue({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="key-value">
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined}>{children}</dd>
    </div>
  );
}

export function Money({ value, className }: { value: unknown; className?: string }) {
  const text = money(value);
  const negative = String(value ?? "").trim().startsWith("-");
  return <span className={cx("money", negative && "negative", className)}>{text}</span>;
}

export function Delta({ value }: { value: unknown }) {
  const number = Number(value);
  if (!Number.isFinite(number)) return <span className="muted">—</span>;
  const positive = number >= 0;
  return (
    <span className={cx("delta", positive ? "positive" : "negative")}>
      {positive ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
      {`${positive ? "+" : ""}${number.toFixed(2)}%`}
    </span>
  );
}

export function Progress({ value, max = 100, tone = "accent" }: { value: number; max?: number; tone?: string }) {
  const width = Math.max(0, Math.min(100, (value / (max || 1)) * 100));
  return (
    <div className="progress" aria-label={`${value} of ${max}`}>
      <span className={`progress-fill tone-${tone}`} style={{ width: `${width}%` }} />
    </div>
  );
}

/* ------------------------------------------------------------------- forms */

export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={cx("field", className)}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function TextInput({
  className,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx("input", className)} {...props} />;
}

export function SearchInput({
  value,
  onChange,
  placeholder = "Search…",
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="search-input">
      <Search size={15} />
      <input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} aria-label={placeholder} />
      {value && (
        <button type="button" onClick={() => onChange("")} aria-label="Clear search">
          <X size={13} />
        </button>
      )}
    </div>
  );
}

export function SelectInput({
  className,
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className={cx("select-wrap", className)}>
      <select className="input select" {...props}>{children}</select>
      <ChevronDown size={14} />
    </span>
  );
}

export function TextArea({
  className,
  ...props
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx("input textarea", className)} {...props} />;
}

/* ------------------------------------------------------------------ modals */

export function Modal({
  open,
  onClose,
  title: heading,
  description,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusable = () =>
      Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) || [],
      ).filter((element) => element.getClientRects().length > 0);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = focusable();
      if (!controls.length) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      const first = controls[0];
      const last = controls[controls.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialogRef.current?.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialogRef.current?.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("modal-open");
    const timer = window.setTimeout(() => focusable()[0]?.focus(), 50);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("modal-open");
      const previous = previousFocusRef.current;
      if (previous && previous.isConnected) previous.focus();
    };
  }, [open]);
  if (!open) return null;
  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div
        className={cx("modal", wide && "modal-wide")}
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        aria-describedby={description ? "modal-description" : undefined}
        tabIndex={-1}
        ref={dialogRef}
      >
        <div className="modal-head">
          <div>
            <div className="eyebrow">NovaMarket operation</div>
            <h2 id="modal-title">{heading}</h2>
            {description && <p id="modal-description">{description}</p>}
          </div>
          <IconButton label="Close dialog" onClick={onClose}><X size={18} /></IconButton>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  open,
  title: heading,
  description,
  confirmLabel = "Confirm",
  tone = "danger",
  busy,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  tone?: "danger" | "primary";
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={heading}
      description={description}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant={tone === "danger" ? "danger" : "primary"} loading={busy} onClick={onConfirm}>{confirmLabel}</Button>
        </>
      }
    >
      <p className="confirm-copy">This operation is authenticated by the NovaMarket backend and recorded in its audit trail.</p>
    </Modal>
  );
}

/* ------------------------------------------------------------------ tables */

export type Column<T> = {
  key: string;
  label: string;
  render?: (row: T) => ReactNode;
  sortable?: boolean;
  align?: "left" | "right" | "center";
  width?: string;
  sortValue?: (row: T) => string | number;
};

export function DataTable<T extends Record<string, any>>({
  columns,
  rows,
  rowKey,
  empty,
  onRowClick,
  compact,
  defaultSort,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index?: number) => string;
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
  compact?: boolean;
  defaultSort?: { key: string; direction: "asc" | "desc" };
}) {
  const [sort, setSort] = useState(defaultSort || null);
  const sorted = useMemo(() => {
    if (!sort) return rows;
    const column = columns.find((item) => item.key === sort.key);
    if (!column) return rows;
    return [...rows].sort((a, b) => {
      const left = column.sortValue ? column.sortValue(a) : a[column.key];
      const right = column.sortValue ? column.sortValue(b) : b[column.key];
      const aNull = left === null || left === undefined || left === "";
      const bNull = right === null || right === undefined || right === "";
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      if (typeof left === "number" && typeof right === "number") {
        return sort.direction === "asc" ? left - right : right - left;
      }
      return sort.direction === "asc"
        ? String(left).localeCompare(String(right))
        : String(right).localeCompare(String(left));
    });
  }, [rows, sort, columns]);

  if (!rows.length && empty) return <>{empty}</>;

  return (
    <div className={cx("table-wrap", compact && "table-compact")}>
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                style={{ width: column.width, textAlign: column.align || "left" }}
                aria-sort={sort?.key === column.key ? (sort.direction === "asc" ? "ascending" : "descending") : column.sortable ? "none" : undefined}
              >
                {column.sortable ? (
                  <button
                    type="button"
                    className={cx("sort-button", sort?.key === column.key && "sort-active")}
                    onClick={() =>
                      setSort((current) =>
                        current?.key === column.key
                          ? { key: column.key, direction: current.direction === "asc" ? "desc" : "asc" }
                          : { key: column.key, direction: "asc" },
                      )
                    }
                  >
                    {column.label}
                    <ChevronDown size={12} className={sort?.key === column.key && sort.direction === "asc" ? "sort-up" : undefined} />
                  </button>
                ) : column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, index) => (
            <tr
              key={rowKey(row, index) || index}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={onRowClick ? "clickable-row" : undefined}
            >
              {columns.map((column) => (
                <td key={column.key} style={{ textAlign: column.align || "left" }}>
                  {column.render ? column.render(row) : (row[column.key] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: string; label: string; count?: number }[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button
          type="button"
          role="tab"
          aria-selected={tab.id === active}
          className={cx("tab", tab.id === active && "tab-active")}
          key={tab.id}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
          {tab.count !== undefined && <span>{tab.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Tooltip({ children, text }: { children: ReactNode; text: string }) {
  return <span className="tooltip" data-tooltip={text}>{children}</span>;
}
