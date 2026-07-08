import { getCache, imageUrl } from "$lib/server/imgcache";
import type { PageServerLoad } from "./$types";

/**
 * Example load function: open a PDF and resolve 3 pages at multiple
 * widths (srcset pattern).
 *
 * Each page is materialised at 2–3 widths so the browser can pick the
 * best variant via <img srcset>. All resolve/sign happens server-side;
 * the browser only sees signed URLs.
 */
export const load: PageServerLoad = async () => {
  const cache = getCache();

  // Example file ID — in production this comes from your database/CMS.
  const fileId = "ac045e19e0574d13";
  const widths = [480, 1024, 1920];

  const pages = await Promise.all(
    [1, 2, 3].map(async (pageNum) => {
      // Build a srcset: same page at 2–3 widths.
      // ImageSpec is immutable — each .scale() returns a new instance.
      const variants = await Promise.all(
        widths.map(async (w) => {
          const spec = cache
            .open(fileId, "application/pdf")
            .page(pageNum, { dpi: 144 })
            .scale({ longestEdge: w })
            .webp({ quality: 82 });
          const url = await imageUrl(spec);
          return { url, w };
        }),
      );

      // Fallback src = middle width.
      const fallback = variants[1] ?? variants[0]!;

      return {
        page: pageNum,
        src: fallback.url,
        srcset: variants.map(({ url, w }) => `${url} ${w}w`).join(", "),
        alt: `Page ${pageNum} of PDF document`,
      };
    }),
  );

  return { pages };
};
