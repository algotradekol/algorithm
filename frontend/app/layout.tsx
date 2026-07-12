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
      <body
        style={{
          fontFamily: "system-ui, sans-serif",
          background: "#0b0f14",
          color: "#e6e6e6",
          margin: 0,
        }}
      >
        {children}
      </body>
    </html>
  );
}
