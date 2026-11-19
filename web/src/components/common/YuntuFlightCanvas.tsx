import React, { useEffect, useRef } from 'react';

interface YuntuFlightCanvasProps {
  interactive?: boolean;
}

export const YuntuFlightCanvas: React.FC<YuntuFlightCanvasProps> = ({ interactive = true }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animId: number;
    let width = 0;
    let height = 0;

    // 航班航迹线与风切点
    const flightTrails: Array<{
      startX: number;
      startY: number;
      endX: number;
      endY: number;
      progress: number;
      speed: number;
      alpha: number;
    }> = [];

    const resize = () => {
      width = window.innerWidth;
      height = window.innerHeight;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      ctx.scale(dpr, dpr);
    };

    resize();
    window.addEventListener('resize', resize);

    // 初始化 12 组航班交错航迹
    for (let i = 0; i < 12; i++) {
      flightTrails.push({
        startX: Math.random() * width * 0.4,
        startY: Math.random() * height,
        endX: width * 0.6 + Math.random() * width * 0.4,
        endY: Math.random() * height,
        progress: Math.random(),
        speed: 0.001 + Math.random() * 0.002,
        alpha: Math.random() * 0.4 + 0.15,
      });
    }

    let mouseX = width / 2;
    let mouseY = height / 2;

    const handleMouseMove = (e: MouseEvent) => {
      if (!interactive) return;
      mouseX = e.clientX;
      mouseY = e.clientY;
    };

    if (interactive) {
      window.addEventListener('mousemove', handleMouseMove);
    }

    const render = () => {
      ctx.clearRect(0, 0, width, height);

      // 背景微温米色网格
      const grad = ctx.createLinearGradient(0, 0, width, height);
      grad.addColorStop(0, '#f7f4ed');
      grad.addColorStop(1, '#eee9de');
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, width, height);

      // 绘制穿梭飞行航迹
      flightTrails.forEach((trail) => {
        trail.progress += trail.speed;
        if (trail.progress > 1) {
          trail.progress = 0;
          trail.startY = Math.random() * height;
          trail.endY = Math.random() * height;
        }

        const currX = trail.startX + (trail.endX - trail.startX) * trail.progress;
        const currY = trail.startY + (trail.endY - trail.startY) * trail.progress;

        // 航迹线渐变
        const lineGrad = ctx.createLinearGradient(trail.startX, trail.startY, currX, currY);
        lineGrad.addColorStop(0, 'rgba(15, 118, 110, 0)');
        lineGrad.addColorStop(0.8, `rgba(15, 118, 110, ${trail.alpha})`);
        lineGrad.addColorStop(1, 'rgba(45, 212, 191, 0.8)');

        ctx.beginPath();
        ctx.moveTo(trail.startX, trail.startY);
        ctx.lineTo(currX, currY);
        ctx.strokeStyle = lineGrad;
        ctx.lineWidth = 1.5;
        ctx.stroke();

        // 飞机头部光斑
        ctx.beginPath();
        ctx.arc(currX, currY, 3, 0, Math.PI * 2);
        ctx.fillStyle = '#166c5b';
        ctx.fill();
      });

      // 鼠标引力光环
      const mouseGrad = ctx.createRadialGradient(mouseX, mouseY, 0, mouseX, mouseY, 180);
      mouseGrad.addColorStop(0, 'rgba(45, 212, 191, 0.12)');
      mouseGrad.addColorStop(1, 'rgba(0, 0, 0, 0)');
      ctx.fillStyle = mouseGrad;
      ctx.beginPath();
      ctx.arc(mouseX, mouseY, 180, 0, Math.PI * 2);
      ctx.fill();

      animId = requestAnimationFrame(render);
    };

    render();

    return () => {
      cancelAnimationFrame(animId);
      window.removeEventListener('resize', resize);
      if (interactive) window.removeEventListener('mousemove', handleMouseMove);
    };
  }, [interactive]);

  return <canvas ref={canvasRef} className="pointer-events-none fixed inset-0 z-0 h-full w-full" />;
};
