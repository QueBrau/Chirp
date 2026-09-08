/** Server-authoritative windows and bounded live invalidations. No raw event rows. */
import { ApiError } from "../api/client";
import { Operation } from "../api/operation";
import { ownsIdentity, type AuthIdentity } from "../auth/identity";

export interface TimedRow { id: string; created_at: string }
export interface WindowCursor { before: string; before_id: string }
export interface WindowState<T> {
  rows: T[]; more: boolean; loading: boolean; loadingMore: boolean;
  failed: boolean; denied: boolean; incomplete: boolean;
}
const fraction = (time: string) => Number((/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(time)?.[1] ?? "").padEnd(6, "0").slice(3, 6));
export const serverOrder = (a: TimedRow, b: TimedRow): number =>
  Date.parse(b.created_at) - Date.parse(a.created_at) || fraction(b.created_at) - fraction(a.created_at)
  || (b.id === a.id ? 0 : b.id < a.id ? -1 : 1);
export function mergeServerRows<T extends TimedRow>(current: readonly T[], incoming: readonly T[]): T[] {
  const rows = new Map(current.map(row => [row.id, row]));
  for (const row of incoming) rows.set(row.id, row);
  return [...rows.values()].sort(serverOrder);
}
interface WindowOptions<T> {
  owner: AuthIdentity;
  selected: () => boolean;
  changed: () => void;
  pageSize: number;
  refreshPages: number;
  page: (cursor: WindowCursor | null, operation: Operation) => Promise<T[]>;
  prepare?: (operation: Operation) => Promise<void>;
  lookup?: (ids: string[], operation: Operation) => Promise<T[]>;
}

/** One read owner per mounted selection. Full refresh replaces cached visibility;
 * live ID selection changes only the requested IDs. Inbox live refresh is a cheap
 * summary head window, preserving older rows outside that covered interval. */
export class DurableWindow<T extends TimedRow> {
  state: WindowState<T> = { rows: [], more: false, loading: true, loadingMore: false, failed: false, denied: false, incomplete: false };
  private cursor: WindowCursor | null = null;
  private active = true;
  private epoch = 0;
  private operation: Operation | null = null;
  private pending = new Set<string>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private livePaused = false;
  private leaving = false;
  activation = 0;

  constructor(private options: WindowOptions<T>) {}
  owns(): boolean { return this.active && this.options.selected() && ownsIdentity(this.options.owner); }
  ownsFocus(activation: number): boolean { return this.owns() && this.activation === activation; }
  activate(): number {
    this.cancel(); this.activation += 1; this.active = true; this.leaving = false;
    return this.activation;
  }
  private publish(update: (state: WindowState<T>) => WindowState<T>): void {
    if (!this.owns()) return;
    this.state = update(this.state);
    this.options.changed();
  }
  private cancel(): void {
    this.epoch += 1;
    this.operation?.cancel(); this.operation = null;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
  retire(): void { this.cancel(); this.pending.clear(); this.active = false; this.activation += 1; }
  private current(operation: Operation, epoch: number): boolean {
    return this.owns() && !this.leaving && this.operation === operation && this.epoch === epoch;
  }
  private failed(error: unknown): void {
    const denied = error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404);
    if (denied) { this.pending.clear(); this.livePaused = true; this.cursor = null; }
    this.publish(state => ({ ...state, ...(denied ? { rows: [], more: false } : {}), denied, failed: true, incomplete: true }));
  }
  private validate(rows: T[]): void {
    if (!Array.isArray(rows) || rows.length > this.options.pageSize || rows.some(row =>
      !row || typeof row.id !== "string" || !row.id || typeof row.created_at !== "string" || !Number.isFinite(Date.parse(row.created_at)))) {
      throw new Error("Invalid server window.");
    }
  }
  private boundary(rows: T[], previous: WindowCursor | null): WindowCursor | null {
    const last = rows.at(-1);
    if (!last) return previous;
    if (previous && serverOrder(last, { id: previous.before_id, created_at: previous.before }) <= 0) {
      throw new Error("Server cursor did not advance.");
    }
    return { before: last.created_at, before_id: last.id };
  }

  async refresh(): Promise<void> {
    if (!this.owns() || this.leaving) return;
    this.cancel(); this.pending.clear(); this.livePaused = false;
    const operation = new Operation({ timeoutMs: 15_000 }, this.options.owner), epoch = this.epoch;
    this.operation = operation;
    this.publish(state => ({ ...state, loading: true, loadingMore: false, failed: false, denied: false, incomplete: false }));
    try {
      if (this.options.prepare) await operation.wait(this.options.prepare(operation));
      let rows: T[] = [], cursor: WindowCursor | null = null, more = false;
      for (let page = 0; page < this.options.refreshPages; page++) {
        operation.assertCurrent();
        const batch = await operation.wait(this.options.page(cursor, operation));
        if (!this.current(operation, epoch)) return;
        this.validate(batch); cursor = this.boundary(batch, cursor);
        rows = mergeServerRows(rows, batch); more = batch.length === this.options.pageSize;
        if (!more) break;
      }
      if (!this.current(operation, epoch)) return;
      this.cursor = cursor;
      this.publish(state => ({ ...state, rows, more }));
    } catch (error) { if (this.current(operation, epoch)) this.failed(error); }
    finally {
      operation.dispose();
      if (this.current(operation, epoch)) {
        this.operation = null;
        this.publish(state => ({ ...state, loading: false }));
        this.scheduleHints();
      }
    }
  }

  async older(): Promise<void> {
    if (!this.owns() || this.leaving || this.operation || !this.state.more || !this.cursor || this.state.denied) return;
    const operation = new Operation({ timeoutMs: 15_000 }, this.options.owner), epoch = this.epoch;
    this.operation = operation;
    this.publish(state => ({ ...state, loadingMore: true }));
    try {
      const rows = await operation.wait(this.options.page(this.cursor, operation));
      if (!this.current(operation, epoch)) return;
      this.validate(rows); this.cursor = this.boundary(rows, this.cursor);
      this.publish(state => ({ ...state, rows: mergeServerRows(state.rows, rows), more: rows.length === this.options.pageSize }));
    } catch (error) { if (this.current(operation, epoch)) this.failed(error); }
    finally {
      operation.dispose();
      if (this.current(operation, epoch)) { this.operation = null; this.publish(state => ({ ...state, loadingMore: false })); this.scheduleHints(); }
    }
  }

  hint(id: string): void {
    if (!this.owns() || this.leaving || this.state.denied || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) return;
    if (this.pending.size >= 50 && !this.pending.has(id)) {
      this.livePaused = true;
      this.publish(state => ({ ...state, incomplete: true }));
      return;
    }
    this.pending.add(id.toLowerCase());
    this.scheduleHints();
  }
  private scheduleHints(): void {
    if (!this.owns() || this.leaving || this.operation || this.timer !== null || this.livePaused || this.pending.size === 0 || this.state.denied) return;
    const epoch = this.epoch;
    const timer = setTimeout(() => {
      if (!this.owns() || this.epoch !== epoch || this.timer !== timer) return;
      this.timer = null; void this.drainHints();
    }, 500);
    this.timer = timer;
  }
  private async drainHints(): Promise<void> {
    if (!this.owns() || this.leaving || this.operation || this.state.denied) return;
    const operation = new Operation({ timeoutMs: 15_000 }, this.options.owner), epoch = this.epoch;
    this.operation = operation;
    try {
      for (let attempt = 0; attempt < 2 && this.pending.size > 0; attempt++) {
        if (attempt) {
          await operation.wait(new Promise<void>(resolve => {
            const timer = setTimeout(() => { if (this.timer === timer) this.timer = null; resolve(); }, 500);
            this.timer = timer;
          }));
        }
        operation.assertCurrent();
        const ids = [...this.pending]; this.pending.clear();
        const rows = await operation.wait(this.options.lookup ? this.options.lookup(ids, operation) : this.options.page(null, operation));
        if (!this.current(operation, epoch)) return;
        this.validate(rows);
        if (this.options.lookup) {
          if (rows.some(row => !ids.includes(row.id))) throw new Error("Invalid message selection.");
          const requested = new Set(ids);
          this.publish(state => ({ ...state, rows: mergeServerRows(state.rows.filter(row => !requested.has(row.id)), rows) }));
        } else {
          const last = rows.at(-1), full = rows.length === this.options.pageSize;
          // Covered head replacement, not a union of all old summary rows.
          this.publish(state => ({ ...state, rows: mergeServerRows(full && last ? state.rows.filter(row => serverOrder(row, last) > 0) : [], rows),
            incomplete: state.incomplete || ids.some(id => !rows.some(row => row.id === id)) }));
        }
      }
      if (this.pending.size > 0) { this.livePaused = true; this.publish(state => ({ ...state, incomplete: true })); }
    } catch (error) {
      if (this.current(operation, epoch)) { this.livePaused = true; this.failed(error); }
    } finally {
      operation.dispose();
      if (this.current(operation, epoch)) this.operation = null;
    }
  }

  async leave(send: (operation: Operation) => Promise<void>, onLeft: () => void): Promise<void> {
    if (!this.owns() || this.leaving) return;
    this.cancel(); this.pending.clear(); this.leaving = true;
    const operation = new Operation({ timeoutMs: 15_000 }, this.options.owner), epoch = this.epoch;
    this.operation = operation;
    try {
      await operation.wait(send(operation));
      if (!this.owns() || this.epoch !== epoch) return;
      this.publish(state => ({ ...state, rows: [], more: false }));
      this.retire(); onLeft();
    } catch (error) { if (this.owns() && this.epoch === epoch) this.failed(error); }
    finally { operation.dispose(); if (this.operation === operation) { this.operation = null; this.leaving = false; } }
  }
}
