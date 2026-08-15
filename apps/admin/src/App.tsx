import { useReducer } from "react";
import { Icon, StatusPill, type IconName } from "@opendle/ui";

type Section =
  | "overview"
  | "topology"
  | "providers"
  | "assignments"
  | "requests"
  | "operations"
  | "security";
type ViewMode = "graph" | "table";

const navigation: {
  id: Section;
  label: string;
  icon: IconName;
  count?: string;
}[] = [
  { id: "overview", label: "Overview", icon: "grid" },
  { id: "topology", label: "Topology", icon: "layers" },
  { id: "providers", label: "Providers & models", icon: "cloud", count: "8" },
  { id: "assignments", label: "Assignments", icon: "activity", count: "12" },
  { id: "requests", label: "Requests", icon: "list" },
  { id: "operations", label: "Operations", icon: "server", count: "2" },
  { id: "security", label: "Security & access", icon: "shield" },
];
const pageTitles: Record<
  Section,
  { eyebrow: string; title: string; description: string }
> = {
  overview: {
    eyebrow: "Global administration",
    title: "Fleet overview",
    description:
      "A calm view of your routing system, ready for the next decision.",
  },
  topology: {
    eyebrow: "Global administration",
    title: "Router topology",
    description:
      "See how traffic moves through healthy nodes and their current boundaries.",
  },
  providers: {
    eyebrow: "Configuration",
    title: "Providers & models",
    description:
      "Manage the catalog and inspect route health without losing context.",
  },
  assignments: {
    eyebrow: "Configuration",
    title: "Assignments",
    description: "Give each work type a clear, observable fallback path.",
  },
  requests: {
    eyebrow: "Observability",
    title: "Request activity",
    description:
      "Follow logical requests from admission to the selected provider.",
  },
  operations: {
    eyebrow: "Operations",
    title: "Operations",
    description:
      "Keep the control plane ready, replicated, and safe to change.",
  },
  security: {
    eyebrow: "Administration",
    title: "Security & access",
    description:
      "Review grants, sessions, and the actions that protect your boundary.",
  },
};
const graphNodes = [
  {
    id: "assignment",
    type: "assignment",
    title: "customer-support",
    subtitle: "Assignment",
    x: 8,
    y: 43,
    status: "active",
  },
  {
    id: "gateway",
    type: "gateway",
    title: "Router gateway",
    subtitle: "2 healthy nodes",
    x: 37,
    y: 43,
    status: "active",
  },
  {
    id: "anthropic",
    type: "provider",
    title: "Anthropic",
    subtitle: "Provider · 3 routes",
    x: 67,
    y: 18,
    status: "active",
  },
  {
    id: "openai",
    type: "provider",
    title: "OpenAI",
    subtitle: "Provider · 4 routes",
    x: 67,
    y: 67,
    status: "active",
  },
  {
    id: "claude",
    type: "model",
    title: "claude-sonnet-4",
    subtitle: "Selected route",
    x: 91,
    y: 18,
    status: "active",
  },
  {
    id: "gpt",
    type: "model",
    title: "gpt-4.1-mini",
    subtitle: "Fallback route",
    x: 91,
    y: 67,
    status: "warning",
  },
];
const activities = [
  {
    time: "2 min ago",
    title: "Fallback route used",
    detail: "gpt-4.1-mini served 2 requests after a rate limit.",
    tone: "amber",
  },
  {
    time: "18 min ago",
    title: "Configuration published",
    detail: "Assignment customer-support is now on revision 42.",
    tone: "lime",
  },
  {
    time: "1 hr ago",
    title: "Provider circuit recovered",
    detail: "Anthropic is healthy after a successful probe.",
    tone: "blue",
  },
];

interface AppState {
  activeSection: Section;
  viewMode: ViewMode;
  selectedNode: string;
  mobileNavOpen: boolean;
  toast: string | null;
}
type AppAction =
  | { type: "section"; value: Section }
  | { type: "view"; value: ViewMode }
  | { type: "node"; value: string }
  | { type: "mobile-nav"; value: boolean }
  | { type: "toast"; value: string | null };
const initialState: AppState = {
  activeSection: "overview",
  viewMode: "graph",
  selectedNode: "claude",
  mobileNavOpen: false,
  toast: null,
};
function appReducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case "section":
      return { ...state, activeSection: action.value, mobileNavOpen: false };
    case "view":
      return { ...state, viewMode: action.value };
    case "node":
      return { ...state, selectedNode: action.value };
    case "mobile-nav":
      return { ...state, mobileNavOpen: action.value };
    case "toast":
      return { ...state, toast: action.value };
  }
}

type Notify = (message: string) => void;

function Sidebar({
  activeSection,
  mobileNavOpen,
  onSection,
  onNotify,
}: {
  activeSection: Section;
  mobileNavOpen: boolean;
  onSection: (section: Section) => void;
  onNotify: Notify;
}) {
  return (
    <aside className={`sidebar ${mobileNavOpen ? "sidebar-open" : ""}`}>
      <div className="brand-lockup">
        <div className="brand-mark" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <div>
          <strong>LLM Router</strong>
          <small>Control plane</small>
        </div>
      </div>
      <button
        className="scope-switcher"
        type="button"
        onClick={() => {
          onNotify(
            "Scope switcher is available in the full administration flow.",
          );
        }}
      >
        <span className="scope-avatar">G</span>
        <span className="scope-copy">
          <strong>Global fleet</strong>
          <small>All services</small>
        </span>
        <Icon name="chevron" size={16} />
      </button>
      <nav className="primary-nav" aria-label="Administration navigation">
        <p className="nav-label">Manage</p>
        {navigation.slice(0, 4).map((item) => (
          <NavItem
            key={item.id}
            item={item}
            active={activeSection === item.id}
            onSection={onSection}
          />
        ))}
        <p className="nav-label nav-label-spaced">Observe</p>
        {navigation.slice(4).map((item) => (
          <NavItem
            key={item.id}
            item={item}
            active={activeSection === item.id}
            onSection={onSection}
            alert={item.id === "operations"}
          />
        ))}
      </nav>
      <div className="sidebar-bottom">
        <div className="sidebar-health">
          <span className="health-beacon" />
          <div>
            <strong>All systems healthy</strong>
            <small>Last checked 30 sec ago</small>
          </div>
        </div>
        <button
          className="sidebar-account"
          type="button"
          onClick={() => {
            onSection("security");
          }}
        >
          <span className="avatar">VL</span>
          <span>
            <strong>Vincent L.</strong>
            <small>Global administrator</small>
          </span>
          <Icon name="more" size={18} />
        </button>
      </div>
    </aside>
  );
}

function NavItem({
  item,
  active,
  alert,
  onSection,
}: {
  item: (typeof navigation)[number];
  active: boolean;
  alert?: boolean;
  onSection: (section: Section) => void;
}) {
  return (
    <button
      className="nav-item"
      data-active={active}
      type="button"
      onClick={() => {
        onSection(item.id);
      }}
    >
      <Icon name={item.icon} size={18} />
      <span>{item.label}</span>
      {item.count ? (
        <b className={alert ? "nav-alert" : undefined}>{item.count}</b>
      ) : null}
    </button>
  );
}

function Topbar({
  page,
  onMobileOpen,
  onSection,
}: {
  page: (typeof pageTitles)[Section];
  onMobileOpen: () => void;
  onSection: (section: Section) => void;
}) {
  return (
    <header className="topbar">
      <button
        className="mobile-menu"
        type="button"
        aria-label="Open navigation"
        onClick={onMobileOpen}
      >
        <Icon name="menu" />
      </button>
      <div className="breadcrumbs">
        <span>Global</span>
        <span className="breadcrumb-separator">/</span>
        <strong>{page.title}</strong>
        <span className="prototype-badge">Prototype</span>
      </div>
      <div className="topbar-actions">
        <label className="search-box">
          <Icon name="search" size={17} />
          <input
            aria-label="Search administration"
            placeholder="Search anything"
          />
          <kbd>⌘ K</kbd>
        </label>
        <button
          className="topbar-icon"
          type="button"
          aria-label="View alerts"
          onClick={() => {
            onSection("operations");
          }}
        >
          <span className="notification-dot" />
          <Icon name="activity" size={18} />
        </button>
        <button
          className="topbar-avatar"
          type="button"
          aria-label="Open security and access"
          onClick={() => {
            onSection("security");
          }}
        >
          VL
        </button>
      </div>
    </header>
  );
}

function PageHeading({
  page,
  onNotify,
}: {
  page: (typeof pageTitles)[Section];
  onNotify: Notify;
}) {
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">{page.eyebrow}</p>
          <h1>{page.title}</h1>
          <p className="page-description">{page.description}</p>
        </div>
        <div className="heading-actions">
          <button
            className="button button-quiet"
            type="button"
            onClick={() => {
              onNotify("Runbook opened in the full administration flow.");
            }}
          >
            <Icon name="eye" size={16} /> View runbook
          </button>
          <button
            className="button button-primary"
            type="button"
            onClick={() => {
              onNotify(
                "Provider creation is ready for the next implementation step.",
              );
            }}
          >
            <Icon name="plus" size={17} /> Add provider
          </button>
        </div>
      </div>
      <section
        className="context-strip"
        aria-label="Current administration scope"
      >
        <ContextItem
          icon="grid"
          tone="context-lime"
          label="Scope"
          value="Global fleet"
        />
        <span className="context-divider" />
        <ContextItem
          icon="layers"
          tone="context-blue"
          label="Services"
          value="3 active"
        />
        <span className="context-divider" />
        <ContextItem
          icon="activity"
          tone="context-purple"
          label="Revision"
          value="router-2026.08.14"
        />
        <div className="context-spacer" />
        <StatusPill tone="lime">Live and synced</StatusPill>
        <span className="context-updated">Updated 30 sec ago</span>
      </section>
    </>
  );
}

function ContextItem({
  icon,
  tone,
  label,
  value,
}: {
  icon: IconName;
  tone: string;
  label: string;
  value: string;
}) {
  return (
    <div className="context-item">
      <span className={`context-icon ${tone}`}>
        <Icon name={icon} size={16} />
      </span>
      <span>
        <small>{label}</small>
        <strong>{value}</strong>
      </span>
    </div>
  );
}

function SummaryStats() {
  return (
    <section className="stat-grid" aria-label="Fleet summary">
      <StatCard
        icon="activity"
        tone="blue"
        label="Requests today"
        value="24,816"
        trend="18.4%"
        note="vs last week"
        spark="sparkline-blue"
      />
      <StatCard
        icon="shield"
        tone="lime"
        label="Success rate"
        value="99.82%"
        trend="0.06%"
        note="vs last week"
        spark="sparkline-lime"
      />
      <StatCard
        icon="clock"
        tone="purple"
        label="Average latency"
        value="842"
        unit="ms"
        trend="12.1%"
        note="faster this week"
        spark="sparkline-purple"
      />
      <StatCard
        icon="database"
        tone="amber"
        label="Spend this month"
        value="$1,284"
        unit=".60"
        trend="68%"
        note="of $1,900 budget"
      />
    </section>
  );
}

function StatCard({
  icon,
  tone,
  label,
  value,
  unit,
  trend,
  note,
  spark,
}: {
  icon: IconName;
  tone: string;
  label: string;
  value: string;
  unit?: string;
  trend: string;
  note: string;
  spark?: string;
}) {
  return (
    <article className="stat-card">
      <div className="stat-top">
        <span className="stat-label">{label}</span>
        <span className={`stat-icon stat-icon-${tone}`}>
          <Icon name={icon} size={17} />
        </span>
      </div>
      <strong>
        {value}
        {unit ? <span className="stat-unit">{unit}</span> : null}
      </strong>
      <span
        className={`stat-trend ${tone === "amber" ? "trend-neutral" : tone === "purple" ? "trend-down" : "trend-up"}`}
      >
        {tone !== "amber" ? <Icon name="arrow-up" size={14} /> : null} {trend}{" "}
        <em>{note}</em>
      </span>
      {spark ? (
        <div className={`sparkline ${spark}`}>
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
        </div>
      ) : (
        <div className="budget-meter">
          <span />
        </div>
      )}
    </article>
  );
}

function GraphPanel({
  selectedNode,
  onNode,
  onNotify,
}: {
  selectedNode: string;
  onNode: (id: string) => void;
  onNotify: Notify;
}) {
  return (
    <div className="graph-body">
      <div className="graph-toolbar">
        <div className="graph-tabs">
          <button className="active" type="button">
            All routes
          </button>
          <button type="button">
            Needs attention <span>2</span>
          </button>
        </div>
        <button
          className="filter-button"
          type="button"
          onClick={() => {
            onNotify(
              "Graph filters are ready for the next implementation step.",
            );
          }}
        >
          <Icon name="filter" size={15} /> Filter
        </button>
      </div>
      <div className="graph-canvas">
        <svg
          className="graph-lines"
          aria-hidden="true"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          <path d="M21 51 C27 51,29 51,37 51" />
          <path d="M54 48 C59 35,61 28,67 25" />
          <path d="M54 54 C59 67,61 72,67 75" />
          <path d="M81 25 C85 25,86 25,91 25" />
          <path d="M81 75 C85 75,86 75,91 75" />
        </svg>
        {graphNodes.map((node) => (
          <button
            key={node.id}
            className={`graph-node node-${node.type} ${selectedNode === node.id ? "is-selected" : ""}`}
            style={{ left: `${String(node.x)}%`, top: `${String(node.y)}%` }}
            type="button"
            onClick={() => {
              onNode(node.id);
            }}
          >
            <span className="node-symbol">
              <Icon
                name={
                  node.type === "assignment"
                    ? "spark"
                    : node.type === "gateway"
                      ? "server"
                      : node.type === "provider"
                        ? "cloud"
                        : "activity"
                }
                size={16}
              />
            </span>
            <span className="node-copy">
              <strong>{node.title}</strong>
              <small>{node.subtitle}</small>
            </span>
            {node.status === "warning" ? (
              <span className="node-warning">
                <Icon name="warning" size={13} />
              </span>
            ) : (
              <span className="node-health" />
            )}
          </button>
        ))}
      </div>
      <div className="graph-legend">
        <span>
          <i className="legend-dot legend-active" /> Active
        </span>
        <span>
          <i className="legend-dot legend-warning" /> Degraded
        </span>
        <span>
          <i className="legend-line" /> Fallback path
        </span>
        <span className="graph-zoom">Scroll to zoom · Drag to pan</span>
      </div>
    </div>
  );
}

function RoutingTable({ onNode }: { onNode: (id: string) => void }) {
  return (
    <div className="table-wrap">
      <table>
        <caption className="sr-only">Routing records</caption>
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Kind</th>
            <th scope="col">Status</th>
            <th scope="col">Last event</th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {graphNodes.map((node) => (
            <tr key={node.id}>
              <td>
                <strong>{node.title}</strong>
                <small>{node.subtitle}</small>
              </td>
              <td>{node.type}</td>
              <td>
                <StatusPill tone={node.status === "warning" ? "amber" : "lime"}>
                  {node.status === "warning" ? "Degraded" : "Healthy"}
                </StatusPill>
              </td>
              <td>
                {node.status === "warning"
                  ? "Rate limit · 2 min ago"
                  : "Checked · 30 sec ago"}
              </td>
              <td>
                <button
                  className="row-action"
                  type="button"
                  aria-label={`Inspect ${node.title}`}
                  onClick={() => {
                    onNode(node.id);
                  }}
                >
                  <Icon name="chevron" size={16} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Inspector({
  onViewTable,
  onNotify,
}: {
  onViewTable: () => void;
  onNotify: Notify;
}) {
  return (
    <aside
      className="panel inspector-panel"
      aria-label="Selected route inspector"
    >
      <div className="inspector-header">
        <div>
          <p className="panel-kicker">Selected route</p>
          <h2>claude-sonnet-4</h2>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="More route actions"
          onClick={() => {
            onNotify("More route actions are available in the full inspector.");
          }}
        >
          <Icon name="more" size={18} />
        </button>
      </div>
      <div className="inspector-status">
        <StatusPill tone="lime">Healthy</StatusPill>
        <span>99.96% over the last 24 hours</span>
      </div>
      <div className="inspector-section">
        <span className="section-label">Effective configuration</span>
        <div className="inheritance-row">
          <span className="inheritance-icon">
            <Icon name="layers" size={15} />
          </span>
          <div>
            <strong>Inherited from Global fleet</strong>
            <small>Revision router-2026.08.14</small>
          </div>
          <Icon name="chevron" size={16} />
        </div>
        <div className="detail-list">
          <div>
            <span>Provider</span>
            <strong>Anthropic</strong>
          </div>
          <div>
            <span>Model</span>
            <strong>claude-sonnet-4</strong>
          </div>
          <div>
            <span>Context window</span>
            <strong>200k tokens</strong>
          </div>
          <div>
            <span>Price</span>
            <strong>$3 / $15 per 1M</strong>
          </div>
        </div>
      </div>
      <div className="inspector-section">
        <span className="section-label">Fallback chain</span>
        <div className="route-chain">
          <ChainStep
            number="1"
            title="claude-sonnet-4"
            detail="Anthropic · Current"
            tone="lime"
            status="Active"
          />
          <div className="chain-line" />
          <ChainStep
            number="2"
            title="gpt-4.1-mini"
            detail="OpenAI · Fallback"
            tone="slate"
            status="Ready"
          />
        </div>
      </div>
      <div className="inspector-alert">
        <Icon name="shield" size={16} />
        <div>
          <strong>Safe to change</strong>
          <span>
            Changes publish after validation and create a new revision.
          </span>
        </div>
      </div>
      <div className="inspector-actions">
        <button
          className="button button-primary button-full"
          type="button"
          onClick={() => {
            onNotify("Route editor opened for claude-sonnet-4.");
          }}
        >
          Edit route <Icon name="chevron" size={16} />
        </button>
        <button
          className="button button-quiet button-full"
          type="button"
          onClick={onViewTable}
        >
          <Icon name="eye" size={16} /> Inspect in table
        </button>
      </div>
    </aside>
  );
}

function ChainStep({
  number,
  title,
  detail,
  tone,
  status,
}: {
  number: string;
  title: string;
  detail: string;
  tone: "lime" | "slate";
  status: string;
}) {
  return (
    <div className={`chain-step ${tone === "lime" ? "current" : ""}`}>
      <span>{number}</span>
      <div>
        <strong>{title}</strong>
        <small>{detail}</small>
      </div>
      <StatusPill tone={tone}>{status}</StatusPill>
    </div>
  );
}

function RoutingWorkspace({
  viewMode,
  selectedNode,
  onView,
  onNode,
  onNotify,
}: {
  viewMode: ViewMode;
  selectedNode: string;
  onView: (mode: ViewMode) => void;
  onNode: (id: string) => void;
  onNotify: Notify;
}) {
  return (
    <section className="workspace-grid">
      <div className="panel graph-panel">
        <div className="panel-header">
          <div>
            <div className="panel-kicker">
              <span className="live-dot" /> Live graph
            </div>
            <h2>Routing graph</h2>
            <p>
              Relationships, fallback paths, and current health at a glance.
            </p>
          </div>
          <div
            className="view-toggle"
            role="group"
            aria-label="Choose graph or table view"
          >
            <button
              className={viewMode === "graph" ? "is-selected" : ""}
              type="button"
              onClick={() => {
                onView("graph");
              }}
            >
              <Icon name="layers" size={15} /> Graph
            </button>
            <button
              className={viewMode === "table" ? "is-selected" : ""}
              type="button"
              onClick={() => {
                onView("table");
              }}
            >
              <Icon name="list" size={15} /> Table
            </button>
          </div>
        </div>
        {viewMode === "graph" ? (
          <GraphPanel
            selectedNode={selectedNode}
            onNode={onNode}
            onNotify={onNotify}
          />
        ) : (
          <RoutingTable onNode={onNode} />
        )}
      </div>
      <Inspector
        onViewTable={() => {
          onView("table");
        }}
        onNotify={onNotify}
      />
    </section>
  );
}

function BottomPanels({
  onSection,
  onNode,
}: {
  onSection: (section: Section) => void;
  onNode: (id: string) => void;
}) {
  return (
    <section className="bottom-grid">
      <div className="panel attention-panel">
        <div className="panel-header compact">
          <div>
            <div className="panel-kicker">Operator focus</div>
            <h2>Needs attention</h2>
          </div>
          <button
            className="text-button"
            type="button"
            onClick={() => {
              onSection("operations");
            }}
          >
            View all <Icon name="chevron" size={15} />
          </button>
        </div>
        <div className="attention-list">
          <button
            className="attention-row"
            type="button"
            onClick={() => {
              onNode("gpt");
            }}
          >
            <span className="attention-icon attention-amber">
              <Icon name="warning" size={16} />
            </span>
            <span>
              <strong>OpenAI fallback is rate limited</strong>
              <small>2 requests used fallback in the last 15 minutes</small>
            </span>
            <Icon name="chevron" size={16} />
          </button>
          <button
            className="attention-row"
            type="button"
            onClick={() => {
              onSection("operations");
            }}
          >
            <span className="attention-icon attention-blue">
              <Icon name="server" size={16} />
            </span>
            <span>
              <strong>Node router-eu-02 needs a drain</strong>
              <small>Maintenance window begins in 2 hours</small>
            </span>
            <Icon name="chevron" size={16} />
          </button>
        </div>
      </div>
      <div className="panel activity-panel">
        <div className="panel-header compact">
          <div>
            <div className="panel-kicker">Audit trail</div>
            <h2>Recent activity</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="More activity actions"
            onClick={() => {
              onSection("requests");
            }}
          >
            <Icon name="more" size={18} />
          </button>
        </div>
        <div className="activity-list">
          {activities.map((item) => (
            <div className="activity-row" key={item.title}>
              <span className={`activity-marker marker-${item.tone}`} />
              <div>
                <strong>{item.title}</strong>
                <small>{item.detail}</small>
              </div>
              <time>{item.time}</time>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function App() {
  const [state, dispatch] = useReducer(appReducer, initialState);
  const page = pageTitles[state.activeSection];
  const notify: Notify = (message) => {
    dispatch({ type: "toast", value: message });
    window.setTimeout(() => {
      dispatch({ type: "toast", value: null });
    }, 2600);
  };
  const selectSection = (section: Section) => {
    dispatch({ type: "section", value: section });
  };
  return (
    <div className="app-shell">
      <Sidebar
        activeSection={state.activeSection}
        mobileNavOpen={state.mobileNavOpen}
        onSection={selectSection}
        onNotify={notify}
      />
      <div className="main-column">
        <Topbar
          page={page}
          onMobileOpen={() => {
            dispatch({ type: "mobile-nav", value: true });
          }}
          onSection={selectSection}
        />
        <main className="content">
          <PageHeading page={page} onNotify={notify} />
          <SummaryStats />
          <RoutingWorkspace
            viewMode={state.viewMode}
            selectedNode={state.selectedNode}
            onView={(mode) => {
              dispatch({ type: "view", value: mode });
            }}
            onNode={(id) => {
              dispatch({ type: "node", value: id });
            }}
            onNotify={notify}
          />
          <BottomPanels
            onSection={selectSection}
            onNode={(id) => {
              dispatch({ type: "node", value: id });
            }}
          />
          <p className="footer-note">
            <Icon name="lock" size={13} /> Mock data only · No request content
            or credentials are shown in this view.
          </p>
        </main>
      </div>
      {state.toast ? (
        <div className="toast" role="status">
          <span className="toast-check">✓</span>
          {state.toast}
        </div>
      ) : null}
    </div>
  );
}

export { App };
