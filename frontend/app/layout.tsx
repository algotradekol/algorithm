import './globals.css';
import AIAssistant from '../components/AIAssistant';

const accentTheme = (process.env.NEXT_PUBLIC_APP_THEME_COLOR || process.env.APP_THEME_COLOR || '').trim().toLowerCase();
const supportedAccentTheme = accentTheme === 'green' ? 'green' : 'blue';

export const metadata = {
  title: "Algo Paper Trading",
  icons: {
    icon: "/icon.svg",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" data-accent-theme={supportedAccentTheme}>
      <head>
        <link href="https://cdn.jsdelivr.net/npm/remixicon@4.5.0/fonts/remixicon.css" rel="stylesheet" />
      </head>
      <body className="min-h-screen font-sans antialiased">
        {children}
        <AIAssistant />
      </body>
    </html>
  );
}
