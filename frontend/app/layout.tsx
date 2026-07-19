import './globals.css';
import AIAssistant from '../components/AIAssistant';

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
    <html lang="en">
      <body className="min-h-screen font-sans antialiased">
        {children}
        <AIAssistant />
      </body>
    </html>
  );
}
