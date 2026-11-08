import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { ReactNode } from 'react';
import { Layout } from './components/Layout';
import { Button, ErrorBox, Loading } from './components/ui';
import { useAuth } from './hooks/auth';
import { AccountPage } from './pages/AccountPage';
import { DashboardPage } from './pages/Dashboard';
import { DevicePage } from './pages/DevicePage';
import { DiscoveryPage } from './pages/DiscoveryPage';
import { ScanPage } from './pages/ScanPage';
import { TemplatesPage } from './pages/TemplatesPage';
import { EventsPage } from './pages/EventsPage';
import { GroupPage } from './pages/GroupPage';
import { LoginPage } from './pages/Login';
import { NotificationsPage } from './pages/NotificationsPage';
import { SensorPage } from './pages/SensorPage';
import { SettingsPage } from './pages/SettingsPage';
import { SetupPage } from './pages/Setup';
import { UsersPage } from './pages/UsersPage';
import { MaintenancePage } from './pages/MaintenancePage';
import { TopologyPage } from './pages/TopologyPage';
import { FindPage } from './pages/FindPage';

function AdminOnly({ children }: { children: ReactNode }) {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  if (!isAdmin) return <ErrorBox error={new Error(t('errors.forbidden'))} />;
  return <>{children}</>;
}

function NotFound() {
  const { t } = useTranslation();
  return <ErrorBox error={new Error(t('errors.not_found'))} />;
}

export function AppRoutes() {
  const { state, bootError, retry } = useAuth();
  const location = useLocation();
  const { t } = useTranslation();

  if (state === 'loading')
    return (
      <div className="flex h-screen items-center justify-center">
        <Loading />
      </div>
    );
  if (state === 'error')
    return (
      <div className="mx-auto mt-24 max-w-md space-y-3 p-4">
        <ErrorBox error={bootError} />
        <Button onClick={retry}>{t('common.retry')}</Button>
      </div>
    );
  if (state === 'setup')
    return (
      <Routes>
        <Route path="/setup" element={<SetupPage />} />
        <Route path="*" element={<Navigate to="/setup" replace />} />
      </Routes>
    );
  if (state === 'anonymous')
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="*"
          element={
            <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
          }
        />
      </Routes>
    );
  return (
    <Routes>
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route path="/setup" element={<Navigate to="/" replace />} />
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="groups/:id" element={<GroupPage />} />
        <Route path="devices/:id" element={<DevicePage />} />
        <Route path="sensors/:id" element={<SensorPage />} />
        <Route path="events" element={<EventsPage />} />
        <Route path="account" element={<AccountPage />} />
        <Route path="templates" element={<TemplatesPage />} />
        <Route path="maintenance" element={<MaintenancePage />} />
        <Route path="topology" element={<TopologyPage />} />
        <Route path="find" element={<FindPage />} />
        <Route
          path="discovery"
          element={
            <AdminOnly>
              <DiscoveryPage />
            </AdminOnly>
          }
        />
        <Route
          path="discovery/scans/:id"
          element={
            <AdminOnly>
              <ScanPage />
            </AdminOnly>
          }
        />
        <Route
          path="notifications"
          element={
            <AdminOnly>
              <NotificationsPage />
            </AdminOnly>
          }
        />
        <Route
          path="settings"
          element={
            <AdminOnly>
              <SettingsPage />
            </AdminOnly>
          }
        />
        <Route
          path="users"
          element={
            <AdminOnly>
              <UsersPage />
            </AdminOnly>
          }
        />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
