/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx}',
    './components/**/*.{js,ts,jsx,tsx}',
    './lib/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        ink: '#07110c',
        panel: '#101a14',
        panelSoft: '#16231b',
        line: '#2a3a30',
        textSoft: '#95a39a',
        success: '#43d17d',
        danger: '#ff6b6b',
        warning: '#f6b35c',
        action: '#1f8f5f',
      },
    },
  },
  plugins: [],
};
