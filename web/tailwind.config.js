/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      screens: {
        xs: "480px",
      },
      colors: {
        /*
         * 色板来源：src/index.css :root 的全局色板（取值以手机端 .ym 为准）。
         * 键名一个都不能删——全站类名依赖它们，这里只改值。
         */
        primary: {
          50: "#eef4f1",
          100: "#dce9d6", // 薄荷
          200: "#c4dbcb",
          300: "#93bfa9",
          400: "#4c9b80",
          500: "#166c5b", // 主绿
          600: "#166c5b", // 主绿：与 500 同一级（原来三种相近的绿收敛）
          700: "#143c35", // 深绿
          800: "#0f2f2a",
          900: "#0a211e",
        },
        accent: {
          50: "#f7efec",
          100: "#efe1db",
          200: "#dfc7bd",
          300: "#c9a394",
          400: "#b57e6b",
          500: "#a35b48", // 警示色基准（原橙色系整体换成这一族）
          600: "#8b4c3c",
          700: "#743f32",
          800: "#5d3328",
          900: "#48271e",
        },
        sand: {
          50: "#f7f5ef", // 纸色底：攻略页 / 历史页背景统一到这里
          100: "#f1efe6",
          200: "#dedfd3", // 分隔线
          300: "#c9cbba",
          400: "#9aa294",
          500: "#65705f", // 次要文字：纸色底上 ≥4.5:1（原 #707d72 只有 3.96）
        },
      },
      maxWidth: {
        form: "34rem",
      },
      borderRadius: {
        xl: "16px",
        "2xl": "20px",
      },
      boxShadow: {
        soft: "0 2px 16px -2px rgba(22, 108, 91, 0.08)",
        card: "0 4px 24px -4px rgba(22, 108, 91, 0.10)",
        "card-hover": "0 8px 32px -4px rgba(22, 108, 91, 0.15)",
      },
      fontFamily: {
        // 字体来源：src/index.css :root 的 --font-* 全局变量
        display: ["var(--font-display)"],
        body: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
    },
  },
  plugins: [],
};


