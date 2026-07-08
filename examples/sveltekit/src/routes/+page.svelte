<script lang="ts">
  let { data } = $props();
</script>

<svelte:head>
  <title>imgcache – SvelteKit Reference</title>
</svelte:head>

<h1>imgcache SvelteKit Reference</h1>

<p>
  Each image below is a PDF page rendered at multiple widths (srcset).
  All URLs are server-resolved and HMAC-signed. On reload the browser
  cache serves every image — zero requests hit the network.
</p>

<section class="pages">
  {#each data.pages as page (page.page)}
    <figure>
      <img
        src={page.src}
        srcset={page.srcset}
        sizes="(max-width: 600px) 480px, (max-width: 1200px) 1024px, 1920px"
        alt={page.alt}
        loading="lazy"
        decoding="async"
        width="1024"
      />
      <figcaption>Page {page.page}</figcaption>
    </figure>
  {/each}
</section>

<style>
  .pages {
    display: grid;
    gap: 2rem;
  }
  figure {
    margin: 0;
  }
  img {
    width: 100%;
    height: auto;
    border: 1px solid #ddd;
    border-radius: 4px;
  }
</style>
