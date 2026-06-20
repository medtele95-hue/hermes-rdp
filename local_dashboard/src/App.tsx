import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { BackendHealthProvider } from './api/BackendHealthContext'
import { OfflineBanner } from './components/OfflineBanner'
import { BtcWarningBanner } from './components/BtcWarningBanner'
import { Sidebar } from './components/layout/Sidebar'
import { TopBar } from './components/layout/TopBar'
import { CommandCenter } from './pages/CommandCenter'
import { QuadTerminal } from './pages/QuadTerminal'
import { LiveMarkets } from './pages/LiveMarkets'
import { SetupHunterPage } from './pages/SetupHunterPage'
import { StrategyEngine } from './pages/StrategyEngine'
import { OrderFlowPage } from './pages/OrderFlowPage'
import { RiskPnL } from './pages/RiskPnL'
import { Trades } from './pages/Trades'
import { AuditSafety } from './pages/AuditSafety'
import { LogsPage } from './pages/LogsPage'
import { Settings } from './pages/Settings'
import { SetupAuditPage } from './pages/SetupAuditPage'

export default function App() {
  return (
    <BackendHealthProvider>
      <BrowserRouter>
        <div className="flex h-screen overflow-hidden bg-surface">
          <Sidebar />
          <div className="flex-1 flex flex-col ml-64">
            <TopBar />
            <OfflineBanner />
            <BtcWarningBanner />
            <main className="flex-1 overflow-y-auto pt-12">
              <Routes>
                <Route path="/" element={<CommandCenter />} />
                <Route path="/quad" element={<QuadTerminal />} />
                <Route path="/markets" element={<LiveMarkets />} />
                <Route path="/setup-hunter" element={<SetupHunterPage />} />
                <Route path="/strategy" element={<StrategyEngine />} />
                <Route path="/order-flow" element={<OrderFlowPage />} />
                <Route path="/risk" element={<RiskPnL />} />
                <Route path="/trades" element={<Trades />} />
                <Route path="/audit" element={<AuditSafety />} />
                <Route path="/logs" element={<LogsPage />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="/setup-audit" element={<SetupAuditPage />} />
              </Routes>
            </main>
          </div>
        </div>
      </BrowserRouter>
    </BackendHealthProvider>
  )
}
