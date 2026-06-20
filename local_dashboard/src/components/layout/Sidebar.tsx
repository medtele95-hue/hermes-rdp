import { NavLink } from 'react-router-dom'
import { clsx } from 'clsx'

const navItems = [
  { path: '/', label: 'Command Center', icon: '⌘' },
  { path: '/quad', label: 'Quad Terminal', icon: '⊞' },
  { path: '/markets', label: 'Live Markets', icon: '◈' },
  { path: '/setup-hunter', label: 'Setup Hunter', icon: '◎' },
  { path: '/strategy', label: 'Strategy Engine', icon: '◇' },
  { path: '/order-flow', label: 'Order Flow', icon: '↕' },
  { path: '/risk', label: 'Risk & PnL', icon: '▦' },
  { path: '/trades', label: 'Trades / Journal', icon: '≡' },
  { path: '/audit', label: 'Audit / Safety', icon: '⊕' },
  { path: '/logs', label: 'Logs', icon: '▤' },
  { path: '/settings', label: 'Settings', icon: '◉' },
  { path: '/setup-audit', label: '48H Setup Audit', icon: '◑' },
]

export function Sidebar() {
  return (
    <aside className="fixed left-0 top-0 bottom-0 w-64 bg-surface-1 border-r border-border z-50 flex flex-col">
      {/* Logo */}
      <div className="h-12 px-4 flex items-center border-b border-border flex-shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-accent-green font-mono font-bold text-lg tracking-widest">HERMES</span>
          <span className="text-2xs text-text-muted font-mono bg-surface-3 px-1.5 py-0.5 rounded border border-border">MT5</span>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-2">
        {navItems.map(({ path, label, icon }) => (
          <NavLink
            key={path}
            to={path}
            end={path === '/'}
            className={({ isActive }) =>
              clsx(
                'flex items-center gap-3 px-4 py-2.5 text-sm transition-colors duration-100',
                isActive
                  ? 'bg-accent-green/10 text-accent-green border-r-2 border-accent-green'
                  : 'text-text-secondary hover:text-text-primary hover:bg-surface-3',
              )
            }
          >
            <span className="text-base font-mono opacity-60 w-5 text-center flex-shrink-0">{icon}</span>
            <span className="font-medium truncate">{label}</span>
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="border-t border-border px-4 py-3 flex-shrink-0">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-accent-green animate-pulse-slow" />
          <span className="text-2xs text-text-muted font-mono">LOCAL DASHBOARD • READ ONLY</span>
        </div>
        <div className="mt-1 text-2xs text-text-muted/60 font-mono">
          localhost:5173
        </div>
      </div>
    </aside>
  )
}
