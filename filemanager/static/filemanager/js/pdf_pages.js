// Renders every page of a PDF into a container, one positioned <div class="sg-page">
// per page, so boxes can be laid over a page in % of its size.
// pdf.js is passed in rather than imported: its URL comes from {% static %}, which
// adds a hash in production that a relative import here could not know.

export async function renderPdfPages(pdfjsLib, container, url) {
    const pdf = await pdfjsLib.getDocument(url).promise;
    container.querySelector('[data-pdf-loading]')?.remove();
    const pageEls = [];
    for (let number = 1; number <= pdf.numPages; number++) {
        const page = await pdf.getPage(number);
        const base = page.getViewport({ scale: 1 });
        // Render sharp at the displayed width; CSS scales the canvas to fit
        const scale = Math.min(container.clientWidth, 900) / base.width * (window.devicePixelRatio || 1);
        const viewport = page.getViewport({ scale });
        const pageEl = document.createElement('div');
        pageEl.className = 'sg-page';
        pageEl.dataset.page = number;
        pageEl.style.aspectRatio = `${viewport.width} / ${viewport.height}`;
        const canvas = document.createElement('canvas');
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        pageEl.appendChild(canvas);
        container.appendChild(pageEl);
        pageEls.push(pageEl);
        await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
    }
    return pageEls;
}

// Places an element over a page; x, y, width and height are 0-1 page ratios
export function placeOnPage(element, pageEl, { x, y, width, height }) {
    pageEl.appendChild(element);
    element.style.left = `${x * 100}%`;
    element.style.top = `${y * 100}%`;
    element.style.width = `${width * 100}%`;
    element.style.height = `${height * 100}%`;
}
