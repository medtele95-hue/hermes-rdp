/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        surface: {
          DEFAULT: '#0d1117',
          1: '#161b22',
          2: '#1c2128',
          3: '#21262d',
          4: '#2d333b',
        },
        border: {
          DEFAULT: '#30363d',
          subtle: '#21262d',
        },
        accent: {
          green: '#3fb950',
          red: '#f85149',
          amber: '#d29922',
          blue: '#388bfd',
          purple: '#bc8cff',
          cyan: '#56d364',
          orange: '#db6d28',
        },
        text: {
          primary: '#e6edf3',
          secondary: '#8b949e',
          muted: '#6e7681',
          link: '#58a6ff',
        },
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', 'monospace'],
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      fontSize: {
        '2xs': ['10px', '14px'],
        xs: ['11px', '16px'],
        sm: ['12px', '18px'],
        base: ['13px', '20px'],
        md: ['14px', '22px'],
        lg: ['15px', '24px'],
        xl: ['17px', '26px'],
        '2xl': ['20px', '28px'],
        '3xl': ['24px', '32px'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'blink': 'blink 1s step-start infinite',
      },
      keyframes: {
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
      },
    },
  },
  plugins: [],
}
