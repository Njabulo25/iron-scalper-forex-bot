/* Minimal SVG charting for the Iron Scalper terminal.
 * No CDN, no build step: line/area and grouped bar charts drawn from the data
 * the API returns, with a shared hover tooltip. Everything re-renders on resize
 * because the SVG is redrawn from the same option object. */
window.ISChart = (function () {
    'use strict';

    var COLORS = {
        gold: '#57d5ca',
        green: '#41d6a1',
        red: '#ff7e91',
        blue: '#7ab6ff',
        dim: '#788f9f'
    };

    var tooltip = document.getElementById('tooltip');

    function showTooltip(event, title, rows) {
        if (!tooltip) return;
        var html = '<strong>' + escapeHtml(title) + '</strong>';
        rows.forEach(function (row) {
            html += '<div class="tt-row"><span>' + escapeHtml(row.label) +
                '</span><span>' + escapeHtml(String(row.value)) + '</span></div>';
        });
        tooltip.innerHTML = html;
        tooltip.hidden = false;
        var pad = 14;
        var width = tooltip.offsetWidth;
        var x = event.clientX + pad;
        if (x + width > window.innerWidth - 8) x = event.clientX - width - pad;
        tooltip.style.left = Math.max(8, x) + 'px';
        tooltip.style.top = Math.max(8, event.clientY - 12) + 'px';
    }

    function hideTooltip() {
        if (tooltip) tooltip.hidden = true;
    }

    function escapeHtml(value) {
        return String(value === undefined || value === null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function el(tag, attrs) {
        var node = document.createElementNS('http://www.w3.org/2000/svg', tag);
        Object.keys(attrs || {}).forEach(function (key) {
            node.setAttribute(key, attrs[key]);
        });
        return node;
    }

    function niceTicks(min, max, count) {
        if (!isFinite(min) || !isFinite(max)) return { min: 0, max: 1, ticks: [0, 1] };
        if (min === max) {
            var pad = Math.abs(min) * 0.1 || 1;
            min -= pad;
            max += pad;
        }
        var span = max - min;
        var step = Math.pow(10, Math.floor(Math.log(span / count) / Math.LN10));
        var err = span / count / step;
        if (err >= 7.5) step *= 10;
        else if (err >= 3.5) step *= 5;
        else if (err >= 1.5) step *= 2;
        var start = Math.ceil(min / step) * step;
        var ticks = [];
        for (var value = start; value <= max + step * 0.001; value += step) {
            ticks.push(Math.abs(value) < step * 1e-6 ? 0 : value);
        }
        return { min: Math.min(min, ticks[0]), max: Math.max(max, ticks[ticks.length - 1]), ticks: ticks };
    }

    function empty(el, text) {
        el.innerHTML = '<div class="empty-state">' + escapeHtml(text || 'No data in this range') + '</div>';
    }

    function frame(container, height) {
        var width = Math.max(container.clientWidth || 600, 260);
        var box = height || container.clientHeight || 240;
        container.innerHTML = '';
        var svg = el('svg', { viewBox: '0 0 ' + width + ' ' + box, preserveAspectRatio: 'none' });
        container.appendChild(svg);
        return { svg: svg, width: width, height: box };
    }

    function attachResize(container, draw) {
        container._chartDraw = draw;
        if (container._chartObserver) return;
        container._chartObserver = new ResizeObserver(function () {
            if (container._chartDraw) container._chartDraw();
        });
        container._chartObserver.observe(container);
    }

    function drawAxes(svg, opts, plot, scaleY, format) {
        var grid = el('g', { class: 'axis' });
        opts.ticks.forEach(function (tick) {
            var y = scaleY(tick);
            grid.appendChild(el('line', {
                class: 'grid-line', x1: plot.left, x2: plot.right, y1: y, y2: y
            }));
            var label = el('text', {
                x: plot.left - 8, y: y + 3, 'text-anchor': 'end'
            });
            label.textContent = format(tick);
            grid.appendChild(label);
        });
        svg.appendChild(grid);
    }

    function drawXLabels(svg, labels, plot, xAt) {
        if (!labels.length) return;
        var group = el('g', { class: 'axis' });
        var max = Math.max(2, Math.min(7, Math.floor(plot.width / 82)));
        var step = Math.ceil(labels.length / max);
        labels.forEach(function (label, index) {
            if (index % step !== 0 && index !== labels.length - 1) return;
            var text = el('text', {
                x: xAt(index), y: plot.bottom + 15, 'text-anchor': 'middle'
            });
            text.textContent = label;
            group.appendChild(text);
        });
        svg.appendChild(group);
    }

    function legend(container, series) {
        if (series.length < 2) return;
        var row = document.createElement('div');
        row.className = 'legend';
        series.forEach(function (item) {
            var span = document.createElement('span');
            span.innerHTML = '<i style="background:' + (item.color || COLORS.gold) + '"></i>' + escapeHtml(item.name);
            row.appendChild(span);
        });
        container.appendChild(row);
    }

    function line(container, options) {
        var series = (options.series || []).filter(function (item) {
            return item.values && item.values.length;
        });
        var labels = options.labels || [];
        if (!series.length || !labels.length) {
            empty(container, options.emptyText);
            return;
        }

        function draw() {
            legend(container, series); // legend is append-only; cleared by frame below
            var view = frame(container, options.height);
            legend(container, series);
            var svg = view.svg;
            var all = series.reduce(function (acc, item) { return acc.concat(item.values); }, []);
            var format = options.format || function (value) { return String(Math.round(value)); };
            var axis = niceTicks(Math.min.apply(null, all), Math.max.apply(null, all), 4);
            if (options.includeZero !== false) {
                axis = niceTicks(Math.min(0, Math.min.apply(null, all)), Math.max(0, Math.max.apply(null, all)), 4);
            }
            var plot = {
                left: 52, right: view.width - 10, top: 12, bottom: view.height - 24
            };
            plot.width = plot.right - plot.left;

            function xAt(index) {
                return labels.length === 1 ? plot.left + plot.width / 2
                    : plot.left + (plot.width * index) / (labels.length - 1);
            }
            function yAt(value) {
                var span = axis.max - axis.min || 1;
                return plot.bottom - ((value - axis.min) / span) * (plot.bottom - plot.top);
            }

            drawAxes(svg, axis, plot, yAt, format);
            drawXLabels(svg, labels, plot, xAt);

            var zero = yAt(0);
            if (axis.min < 0 && axis.max > 0) {
                svg.appendChild(el('line', {
                    x1: plot.left, x2: plot.right, y1: zero, y2: zero, stroke: '#2b343e'
                }));
            }

            series.forEach(function (item, sIndex) {
                var color = item.color || [COLORS.gold, COLORS.blue, COLORS.green, COLORS.red][sIndex % 4];
                var points = item.values.map(function (value, index) {
                    return [xAt(index), yAt(value)];
                });
                var path = points.map(function (point, index) {
                    return (index ? 'L' : 'M') + point[0].toFixed(2) + ' ' + point[1].toFixed(2);
                }).join(' ');

                if (item.fill !== false) {
                    var gradientId = 'fill-' + Math.random().toString(36).slice(2, 9);
                    var defs = el('defs');
                    var gradient = el('linearGradient', {
                        id: gradientId, x1: '0', x2: '0', y1: '0', y2: '1'
                    });
                    gradient.appendChild(el('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': '0.22' }));
                    gradient.appendChild(el('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': '0' }));
                    defs.appendChild(gradient);
                    svg.appendChild(defs);
                    var area = path + ' L' + points[points.length - 1][0].toFixed(2) + ' ' + zero.toFixed(2) +
                        ' L' + points[0][0].toFixed(2) + ' ' + zero.toFixed(2) + ' Z';
                    svg.appendChild(el('path', { d: area, fill: 'url(#' + gradientId + ')' }));
                }

                svg.appendChild(el('path', {
                    d: path, fill: 'none', stroke: color, 'stroke-width': '1.7',
                    'stroke-linejoin': 'round', 'stroke-linecap': 'round'
                }));
            });

            var crosshair = el('line', {
                y1: plot.top, y2: plot.bottom, stroke: '#3b4652', 'stroke-dasharray': '3 3', opacity: '0'
            });
            var dots = series.map(function (item, index) {
                var dot = el('circle', {
                    r: '3.2', fill: item.color || [COLORS.gold, COLORS.blue][index % 2], opacity: '0'
                });
                svg.appendChild(dot);
                return dot;
            });
            svg.appendChild(crosshair);

            var capture = el('rect', {
                x: plot.left, y: plot.top, width: plot.width, height: plot.bottom - plot.top,
                fill: 'transparent', style: 'cursor:crosshair'
            });
            svg.appendChild(capture);

            capture.addEventListener('pointermove', function (event) {
                var rect = svg.getBoundingClientRect();
                var ratio = (event.clientX - rect.left) / rect.width;
                var index = Math.round(ratio * view.width - plot.left);
                index = Math.max(0, Math.min(labels.length - 1,
                    Math.round((index / plot.width) * (labels.length - 1))));
                var x = xAt(index);
                crosshair.setAttribute('x1', x);
                crosshair.setAttribute('x2', x);
                crosshair.setAttribute('opacity', '1');
                series.forEach(function (item, sIndex) {
                    dots[sIndex].setAttribute('cx', x);
                    dots[sIndex].setAttribute('cy', yAt(item.values[index] || 0));
                    dots[sIndex].setAttribute('opacity', '1');
                });
                showTooltip(event, labels[index], series.map(function (item) {
                    return { label: item.name, value: format(item.values[index] || 0) };
                }));
            });
            capture.addEventListener('pointerleave', function () {
                crosshair.setAttribute('opacity', '0');
                dots.forEach(function (dot) { dot.setAttribute('opacity', '0'); });
                hideTooltip();
            });
        }

        attachResize(container, draw);
        draw();
    }

    function bars(container, options) {
        var series = (options.series || []).filter(function (item) {
            return item.values && item.values.length;
        });
        var labels = options.labels || [];
        if (!series.length || !labels.length) {
            empty(container, options.emptyText);
            return;
        }

        function draw() {
            var view = frame(container, options.height);
            var svg = view.svg;
            var format = options.format || function (value) { return String(Math.round(value)); };
            var all = series.reduce(function (acc, item) { return acc.concat(item.values); }, []);
            var axis = niceTicks(Math.min(0, Math.min.apply(null, all)), Math.max(0, Math.max.apply(null, all)), 4);
            var plot = { left: 52, right: view.width - 10, top: 12, bottom: view.height - 24 };
            plot.width = plot.right - plot.left;

            function yAt(value) {
                var span = axis.max - axis.min || 1;
                return plot.bottom - ((value - axis.min) / span) * (plot.bottom - plot.top);
            }

            drawAxes(svg, axis, plot, yAt, format);
            drawXLabels(svg, labels, plot, function (index) {
                return plot.left + (plot.width * (index + 0.5)) / labels.length;
            });

            var zero = yAt(0);
            svg.appendChild(el('line', {
                x1: plot.left, x2: plot.right, y1: zero, y2: zero, stroke: '#2b343e'
            }));

            var slot = plot.width / labels.length;
            var groupWidth = Math.min(slot * 0.62, 26);
            var barWidth = groupWidth / series.length;

            labels.forEach(function (label, index) {
                series.forEach(function (item, sIndex) {
                    var value = item.values[index] || 0;
                    var color = item.color || (value >= 0 ? COLORS.green : COLORS.red);
                    var top = Math.min(yAt(value), zero);
                    var height = Math.max(Math.abs(yAt(value) - zero), 0.8);
                    var x = plot.left + slot * index + (slot - groupWidth) / 2 + barWidth * sIndex;
                    var rect = el('rect', {
                        x: x.toFixed(1), y: top.toFixed(1), width: Math.max(barWidth - 1.5, 1).toFixed(1),
                        height: height.toFixed(1), rx: '2', fill: color, opacity: '0.9'
                    });
                    rect.addEventListener('pointermove', function (event) {
                        showTooltip(event, label, series.map(function (other) {
                            var otherValue = other.values[index] || 0;
                            return { label: other.name, value: format(otherValue) };
                        }));
                    });
                    rect.addEventListener('pointerleave', hideTooltip);
                    svg.appendChild(rect);
                });
            });
        }

        attachResize(container, draw);
        draw();
    }

    return { line: line, bars: bars, colors: COLORS };
})();
