import type { Settings } from '../../api/types';
import { featuresFromSettings } from '../../lib/features';
import { AwsConnectionsCard } from './AwsConnectionsCard';

/**
 * [4] Settings → AWS (architecture § 15.1.10): always visible, no feature toggle — a
 * connection is infrastructure and inert while nothing uses it.
 */
export function AwsTab({ settings }: { settings: Settings }) {
  const f = featuresFromSettings(settings);
  return (
    <div className="space-y-6" data-testid="aws-settings">
      <AwsConnectionsCard assistantOn={f.assistant} />
    </div>
  );
}
