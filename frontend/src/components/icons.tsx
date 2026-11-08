import type { SVGProps } from 'react';

type P = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 16, children, ...rest }: P & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const ChevronIcon = (p: P) => (
  <Svg {...p}>
    <path d="M9 6l6 6-6 6" />
  </Svg>
);
export const MenuIcon = (p: P) => (
  <Svg {...p}>
    <path d="M4 6h16M4 12h16M4 18h16" />
  </Svg>
);
export const FolderIcon = (p: P) => (
  <Svg {...p}>
    <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
  </Svg>
);
export const ServerIcon = (p: P) => (
  <Svg {...p}>
    <rect x="3" y="4" width="18" height="7" rx="1.5" />
    <rect x="3" y="13" width="18" height="7" rx="1.5" />
    <path d="M7 7.5h.01M7 16.5h.01" />
  </Svg>
);
export const PulseIcon = (p: P) => (
  <Svg {...p}>
    <path d="M3 12h4l3-7 4 14 3-7h4" />
  </Svg>
);
export const DashboardIcon = (p: P) => (
  <Svg {...p}>
    <rect x="3" y="3" width="7" height="9" rx="1" />
    <rect x="14" y="3" width="7" height="5" rx="1" />
    <rect x="14" y="12" width="7" height="9" rx="1" />
    <rect x="3" y="16" width="7" height="5" rx="1" />
  </Svg>
);
export const ListIcon = (p: P) => (
  <Svg {...p}>
    <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
  </Svg>
);
export const BellIcon = (p: P) => (
  <Svg {...p}>
    <path d="M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 01-3.4 0" />
  </Svg>
);
export const CogIcon = (p: P) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z" />
  </Svg>
);
export const UsersIcon = (p: P) => (
  <Svg {...p}>
    <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M23 21v-2a4 4 0 00-3-3.9M16 3.1a4 4 0 010 7.8" />
  </Svg>
);
export const PlusIcon = (p: P) => (
  <Svg {...p}>
    <path d="M12 5v14M5 12h14" />
  </Svg>
);
export const PlayIcon = (p: P) => (
  <Svg {...p}>
    <path d="M6 4l14 8-14 8z" />
  </Svg>
);
export const PauseIcon = (p: P) => (
  <Svg {...p}>
    <path d="M7 4h3v16H7zM14 4h3v16h-3z" />
  </Svg>
);
export const TrashIcon = (p: P) => (
  <Svg {...p}>
    <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" />
  </Svg>
);
export const EditIcon = (p: P) => (
  <Svg {...p}>
    <path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z" />
  </Svg>
);
export const RefreshIcon = (p: P) => (
  <Svg {...p}>
    <path d="M21 12a9 9 0 11-3-6.7L21 8M21 3v5h-5" />
  </Svg>
);
export const SunIcon = (p: P) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </Svg>
);
export const MoonIcon = (p: P) => (
  <Svg {...p}>
    <path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" />
  </Svg>
);
export const MonitorIcon = (p: P) => (
  <Svg {...p}>
    <rect x="2" y="3" width="20" height="14" rx="2" />
    <path d="M8 21h8M12 17v4" />
  </Svg>
);
export const LogoutIcon = (p: P) => (
  <Svg {...p}>
    <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
  </Svg>
);
export const KeyIcon = (p: P) => (
  <Svg {...p}>
    <circle cx="7.5" cy="15.5" r="4.5" />
    <path d="M10.7 12.3L21 2M16 7l3 3M19 4l2 2" />
  </Svg>
);
export const XIcon = (p: P) => (
  <Svg {...p}>
    <path d="M18 6L6 18M6 6l12 12" />
  </Svg>
);
export const SearchIcon = (p: P) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="M21 21l-4.3-4.3" />
  </Svg>
);
export const GlobeIcon = (p: P) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="10" />
    <path d="M2 12h20M12 2a15 15 0 010 20M12 2a15 15 0 000 20" />
  </Svg>
);

// [2a] discovery / device classes
export const RadarIcon = (p: P) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <circle cx="12" cy="12" r="5" />
    <path d="M12 12l6-6" />
    <circle cx="12" cy="12" r="1" fill="currentColor" />
  </Svg>
);
export const LayersIcon = (p: P) => (
  <Svg {...p}>
    <path d="M12 3l9 5-9 5-9-5 9-5z" />
    <path d="M3 13l9 5 9-5" />
  </Svg>
);
export const RouterIcon = (p: P) => (
  <Svg {...p}>
    <rect x="3" y="13" width="18" height="7" rx="2" />
    <path d="M7 16.5h.01M11 16.5h.01M17 13V9M14 7a4 4 0 016 0M12 5a7 7 0 0110 0" />
  </Svg>
);
export const SwitchIcon = (p: P) => (
  <Svg {...p}>
    <rect x="2" y="7" width="20" height="10" rx="2" />
    <path d="M6 11h2v2H6zM10 11h2v2h-2zM14 11h2v2h-2zM18 11h.01" />
  </Svg>
);
export const WifiIcon = (p: P) => (
  <Svg {...p}>
    <path d="M2 9a15 15 0 0120 0M5 12.5a10 10 0 0114 0M8.5 16a5 5 0 017 0" />
    <circle cx="12" cy="19" r="1" fill="currentColor" />
  </Svg>
);
export const PrinterIcon = (p: P) => (
  <Svg {...p}>
    <path d="M6 9V3h12v6" />
    <rect x="3" y="9" width="18" height="8" rx="2" />
    <path d="M6 14h12v7H6z" />
  </Svg>
);
export const NasIcon = (p: P) => (
  <Svg {...p}>
    <rect x="5" y="3" width="14" height="18" rx="2" />
    <path d="M8 7h8M8 11h8M8 15h8" />
    <circle cx="12" cy="18.5" r=".5" fill="currentColor" />
  </Svg>
);
export const DesktopIcon = (p: P) => (
  <Svg {...p}>
    <rect x="3" y="4" width="18" height="12" rx="2" />
    <path d="M8 20h8M12 16v4" />
  </Svg>
);
export const QuestionIcon = (p: P) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M9.5 9.5a2.5 2.5 0 015 .5c0 1.5-2.5 2-2.5 3.5M12 17h.01" />
  </Svg>
);
export const CheckIcon = (p: P) => (
  <Svg {...p}>
    <path d="M5 12l5 5L20 7" />
  </Svg>
);
export const StopIcon = (p: P) => (
  <Svg {...p}>
    <rect x="6" y="6" width="12" height="12" rx="1" />
  </Svg>
);

// [2b-1]
/** Raw SVG inner markup (24×24, stroke-based) shared by React icons and the topology symbols. */
export const WRENCH_SVG =
  '<path d="M14.7 6.3a4 4 0 00-5.4 5.2L3.6 17.2a1.6 1.6 0 002.2 2.2l5.7-5.7a4 4 0 005.2-5.4l-2.5 2.5-2.1-.5-.5-2.1z"/>';
export const WrenchIcon = (p: P) => (
  <Svg {...p}>
    <path d="M14.7 6.3a4 4 0 00-5.4 5.2L3.6 17.2a1.6 1.6 0 002.2 2.2l5.7-5.7a4 4 0 005.2-5.4l-2.5 2.5-2.1-.5-.5-2.1z" />
  </Svg>
);
export const MapIcon = (p: P) => (
  <Svg {...p}>
    <circle cx="5" cy="6" r="2.5" />
    <circle cx="19" cy="6" r="2.5" />
    <circle cx="12" cy="18" r="2.5" />
    <path d="M7.5 6h9M6.2 8.2l4.6 7.6M17.8 8.2l-4.6 7.6" />
  </Svg>
);
export const LinkIcon = (p: P) => (
  <Svg {...p}>
    <path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1" />
  </Svg>
);
export const EscalateIcon = (p: P) => (
  <Svg {...p}>
    <path d="M12 19V5M5 12l7-7 7 7M5 21h14" />
  </Svg>
);
export const FingerprintIcon = (p: P) => (
  <Svg {...p}>
    <path d="M12 11v3a8 8 0 01-1.5 4.5M8 12a4 4 0 018 0v1a12 12 0 01-.7 4M5 15a10 10 0 01-.5-3 7.5 7.5 0 0115 0v1M17.5 18.5l.3-1.5M7 19a8 8 0 001-3.5" />
  </Svg>
);
export const ShieldCheckIcon = (p: P) => (
  <Svg {...p}>
    <path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3z" />
    <path d="M9 12l2 2 4-4" />
  </Svg>
);
