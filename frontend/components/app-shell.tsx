"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Building2,
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { useAuth } from "@/lib/auth-context";
import { Logo } from "@/components/brand";
import { MAIN_CONTENT_ID, SkipToContent } from "@/components/page-primitives";
import { WorkspaceContentBoundary } from "@/components/workspace-boundary";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";

export interface NavItem {
  href: string;
  label: string;
  icon?: LucideIcon;
  exact?: boolean;
  /**
   * A count rendered beside the label, e.g. unread Updates. Omit or pass 0 to
   * render nothing: a badge showing zero is furniture, and the whole value of
   * a badge is that its presence means something.
   */
  badge?: number;
}

function isActive(pathname: string, item: NavItem) {
  return item.exact
    ? pathname === item.href
    : pathname === item.href || pathname.startsWith(item.href + "/");
}

/**
 * One navigation link, shared by the desktop rail and the mobile sheet.
 *
 * The active item is a solid brand pill; every other item is plain ink with a
 * brand-tinted hover. Nothing here is grey: an inactive item differs from an
 * active one by background and weight, never by a dimmed text colour.
 */
function NavLink({
  item,
  active,
  onNavigate,
  collapsed = false,
}: {
  item: NavItem;
  active: boolean;
  onNavigate?: () => void;
  /** Icon-only rendering for the collapsed rail (directive Part 1 section 9).
   * The label survives for assistive tech and as the hover tooltip. */
  collapsed?: boolean;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      title={collapsed ? item.label : undefined}
      className={cn(
        "flex items-center gap-3 rounded-lg text-sm transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        collapsed ? "justify-center px-0 py-2.5" : "px-3 py-2.5",
        active
          ? "bg-brand-600 font-semibold text-white shadow-brand"
          : "font-medium hover:bg-brand-100/70 hover:text-accent-foreground"
      )}
    >
      {Icon ? <Icon className="h-4 w-4 shrink-0" aria-hidden="true" /> : null}
      <span className={cn("truncate", collapsed && "sr-only")}>{item.label}</span>
      {item.badge ? (
        <span
          className={cn(
            "ml-auto shrink-0 rounded-full px-1.5 py-0.5 text-chip font-semibold leading-none",
            active ? "bg-white text-black dark:bg-black dark:text-white" : "bg-brand-600 text-white",
            // In the collapsed rail there is no label to sit beside, so the
            // count rides the icon rather than disappearing with the text.
            collapsed && "ml-0",
          )}
        >
          {item.badge > 9 ? "9+" : item.badge}
          <span className="sr-only"> unread</span>
        </span>
      ) : null}
    </Link>
  );
}

function AccountBlock({ compact = false }: { compact?: boolean }) {
  const { user, logout } = useAuth();
  return (
    <div className={cn("space-y-3", compact ? "p-4" : "p-4")}>
      {user ? (
        <>
          <div className="flex items-center gap-3 rounded-lg bg-secondary px-3 py-2.5">
            <span
              aria-hidden="true"
              className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-brand-600 text-xs font-semibold text-white"
            >
              {(user.full_name || user.email || "?").slice(0, 1).toUpperCase()}
            </span>
            <div className="min-w-0">
              <p className="truncate text-body-sm font-semibold">
                {user.full_name || user.email}
              </p>
              <p className="truncate text-meta">{user.email}</p>
            </div>
          </div>
          <WorkspaceSwitcher />
        </>
      ) : null}
      <Button
        variant="outline"
        size="sm"
        className="w-full justify-start gap-2"
        onClick={() => void logout()}
      >
        <LogOut className="h-4 w-4" aria-hidden="true" /> Sign out
      </Button>
    </div>
  );
}

/**
 * The signed-in application shell: Customer Portal, Candidate Portal and the
 * Provider Portal all render inside this.
 *
 * Layout is a fixed 264px rail on `md` and up, and a slide-over sheet below it,
 * so the shell is usable from 360px without the content ever scrolling
 * sideways. Glass is used on the mobile top bar only, per DESIGN_BRIEF rule 7,
 * and never behind a form or a table.
 */
export function AppShell({
  title,
  nav,
  children,
}: {
  title: string;
  nav: NavItem[];
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, loading, logout } = useAuth();
  const [open, setOpen] = React.useState(false);

  // Expandable / collapsible desktop rail (directive Part 1 sections 9-10).
  // The choice persists per browser; it is a personal convenience, not a
  // customization system. Read inside an effect so server render and first
  // client paint agree (expanded), then the stored preference applies.
  const [collapsed, setCollapsed] = React.useState(false);
  React.useEffect(() => {
    try {
      setCollapsed(
        window.localStorage.getItem("pickready-sidebar-collapsed") === "1"
      );
    } catch {
      // Storage unavailable (private mode): stay expanded.
    }
  }, []);
  const toggleCollapsed = () => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(
          "pickready-sidebar-collapsed",
          next ? "1" : "0"
        );
      } catch {
        // Non-persistent is fine; the toggle still works for the session.
      }
      return next;
    });
  };

  React.useEffect(() => {
    if (!loading && !user) {
      // Remember where they were. This used to be a bare `/login`, which is
      // what made an emailed assessment link land the candidate on the jobs
      // board after signing in: they had been sent to the right page, the
      // shell bounced them, and the destination was thrown away on the way
      // out. `finish()` in the login flow only honours a same-origin path, so
      // this cannot be turned into an open redirect.
      const here =
        typeof window === "undefined"
          ? pathname
          : window.location.pathname + window.location.search;
      router.replace(`/login?next=${encodeURIComponent(here)}`);
    }
  }, [loading, user, router, pathname]);

  // Close the mobile sheet whenever the route changes, otherwise it stays open
  // over the page the user just navigated to.
  React.useEffect(() => {
    setOpen(false);
  }, [pathname]);

  const railNav = (onNavigate?: () => void, railCollapsed = false) => (
    <nav
      className={cn(
        "flex-1 space-y-1 overflow-y-auto",
        railCollapsed ? "p-2" : "p-3"
      )}
      aria-label={title}
    >
      {nav.map((item) => (
        <NavLink
          key={item.href}
          item={item}
          active={isActive(pathname, item)}
          onNavigate={onNavigate}
          collapsed={railCollapsed}
        />
      ))}
    </nav>
  );

  return (
    <div className="flex min-h-screen bg-canvas text-foreground">
      <SkipToContent />
      <aside
        className={cn(
          "fixed inset-y-0 left-0 hidden shrink-0 flex-col border-r border-border bg-surface transition-[width] duration-150 md:flex",
          collapsed ? "w-[72px]" : "w-[264px]"
        )}
      >
        <div className={cn(collapsed ? "px-3 py-6" : "px-5 py-6")}>
          {collapsed ? (
            <div className="flex justify-center">
              <Logo variant="mark" height={30} href="/" />
            </div>
          ) : (
            <>
              <Logo variant="full" height={34} href="/" />
              <p className="mt-3 type-eyebrow">
                {title}
              </p>
              {user ? (
                <div
                  className="mt-3 flex items-center gap-2 rounded-lg border border-brand-600/20 bg-brand-100/60 px-3 py-2"
                  data-active-workspace={user.workspace_name}
                >
                  <Building2 className="h-4 w-4 shrink-0 text-brand-600" aria-hidden="true" />
                  <div className="min-w-0">
                    <p className="type-eyebrow">
                      Active workspace
                    </p>
                    <p className="truncate text-sm font-semibold leading-5">{user.workspace_name}</p>
                  </div>
                </div>
              ) : null}
            </>
          )}
        </div>
        <Separator />
        {railNav(undefined, collapsed)}
        <Separator />
        <div className={cn("flex", collapsed ? "justify-center p-2" : "px-3 pt-3")}>
          <Button
            variant="ghost"
            size={collapsed ? "icon" : "sm"}
            className={cn(!collapsed && "w-full justify-start gap-2")}
            aria-pressed={collapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={toggleCollapsed}
          >
            {collapsed ? (
              <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
            ) : (
              <>
                <PanelLeftClose className="h-4 w-4" aria-hidden="true" />
                Collapse
              </>
            )}
          </Button>
        </div>
        {collapsed ? (
          <div className="flex justify-center p-2 pb-4">
            <Button
              variant="ghost"
              size="icon"
              aria-label="Sign out"
              title="Sign out"
              onClick={() => void logout()}
            >
              <LogOut className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        ) : (
          <AccountBlock />
        )}
      </aside>

      <div
        className={cn(
          "flex min-w-0 flex-1 flex-col transition-[padding] duration-150",
          collapsed ? "md:pl-[72px]" : "md:pl-[264px]"
        )}
      >
        <header className="glass sticky top-0 z-40 flex h-16 items-center gap-3 border-b border-border px-4 md:hidden">
          <Sheet open={open} onOpenChange={setOpen}>
            <SheetTrigger asChild>
              <Button variant="outline" size="icon" aria-label="Open menu">
                <Menu className="h-5 w-5" aria-hidden="true" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="flex w-[280px] flex-col p-0">
              <SheetTitle className="sr-only">{title}</SheetTitle>
              <div className="px-5 py-6">
                <Logo variant="full" height={32} />
                <p className="mt-3 type-eyebrow">
                  {title}
                </p>
              </div>
              <Separator />
              {railNav(() => setOpen(false))}
              <Separator />
              <AccountBlock compact />
            </SheetContent>
          </Sheet>
          <Logo variant="mark" height={32} href="/" />
          <span className="min-w-0 truncate text-sm font-semibold">
            {user?.workspace_name ?? title}
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto"
            aria-label="Sign out"
            onClick={() => void logout()}
          >
            <LogOut className="h-4 w-4" aria-hidden="true" />
          </Button>
        </header>

        <main
          id={MAIN_CONTENT_ID}
          tabIndex={-1}
          className="mx-auto w-full max-w-[1400px] flex-1 px-6 py-6 outline-none md:px-10 md:py-8"
        >
          <WorkspaceContentBoundary user={user}>
            {loading && !user ? <ShellSkeleton /> : children}
          </WorkspaceContentBoundary>
        </main>
      </div>
    </div>
  );
}

function ShellSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" className="space-y-6">
      <Skeleton className="h-9 w-56 rounded-lg" />
      <Skeleton className="h-4 w-80 rounded" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Skeleton className="h-28 rounded-xl" />
        <Skeleton className="h-28 rounded-xl" />
        <Skeleton className="h-28 rounded-xl" />
      </div>
      <Skeleton className="h-64 w-full rounded-xl" />
      <span className="sr-only">Loading</span>
    </div>
  );
}

/**
 * The one page title block. Every screen in every portal opens with this, so
 * the eye lands in the same place on every route.
 *
 * `eyebrow` is the small brand-coloured kicker above the title (it is the only
 * place a non-ink text colour is allowed, and it carries no information a
 * colour-blind reader would lose). `description` is one short line about the
 * task, never a paragraph explaining how the feature is implemented.
 */
export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
  className,
}: {
  eyebrow?: string;
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "mb-8 flex flex-wrap items-start justify-between gap-4 border-b border-border pb-6",
        className
      )}
    >
      <div className="min-w-0">
        {eyebrow ? (
          <p className="type-eyebrow text-brand-600">{eyebrow}</p>
        ) : null}
        <h1
          className={cn(
            "type-page-title text-balance",
            eyebrow && "mt-2"
          )}
        >
          {title}
        </h1>
        {description ? (
          <p className="mt-2.5 max-w-prose text-pretty text-body">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}
