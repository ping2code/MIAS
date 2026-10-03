import { useEffect, useRef, type KeyboardEvent, type RefObject } from "react";
import { NavLink } from "react-router";

/**
 * The navigation drawer used at tablet and phone widths (Phase 16E). A modal dialog: focus moves to the first link
 * on open and is trapped inside; Escape, the close button and the backdrop close it and return focus to the menu
 * button; choosing a destination closes it and the new page's heading takes focus. The rest of the shell is made
 * `inert` by the AppShell while it is open.
 */
export function NavDrawer({
  items,
  onClose,
  triggerRef,
}: {
  items: readonly { to: string; label: string }[];
  onClose: (opts: { returnFocus: boolean }) => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    panel.current?.querySelector<HTMLElement>("a, button")?.focus();
    const trigger = triggerRef.current;
    return () => {
      // If focus would otherwise be lost (e.g. it was inside the drawer), put it back on the trigger.
      if (document.activeElement === document.body || document.activeElement === null) trigger?.focus();
    };
  }, [triggerRef]);

  const onKeyDown = (event: KeyboardEvent): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose({ returnFocus: true });
      return;
    }
    if (event.key !== "Tab" || !panel.current) return;
    const focusable = Array.from(panel.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled])"));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="drawer-layer">
      <div
        className="drawer-backdrop"
        aria-hidden="true"
        onClick={() => {
          onClose({ returnFocus: true });
        }}
      />
      <div
        ref={panel}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        id="nav-drawer"
        onKeyDown={onKeyDown}
      >
        <div className="drawer-header">
          <h2 id="drawer-title" className="drawer-title">
            Navigation
          </h2>
          <button
            type="button"
            className="button button-quiet button-small"
            onClick={() => {
              onClose({ returnFocus: true });
            }}
          >
            Close <span aria-hidden="true">✕</span>
          </button>
        </div>
        <nav aria-label="Primary">
          <ul className="drawer-list">
            {items.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.to === "/"}
                  onClick={() => {
                    onClose({ returnFocus: false });
                  }}
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </div>
  );
}
