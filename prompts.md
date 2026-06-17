# Prompts Log

A running record of the textual prompts given in this project.

## Session 1 (scraper development)

1. I need to produce a separate script that will utilize simple scraping with https://github.com/adbar/trafilatura repo to save the text data from the page. The script should do the following 1. go to the accessibility.csv and go only for with status equals to True and save the content of main page. Make the script that way so it always save scraped content and not start from the beggining.

2. I need to fix the code and wait until page is fully loaded and all redirection happen so the scraping done fully and only after that proceed to the next one.

3. please ovewrite

## Session 2 (prompt tracking)

4. Can you show previous prompts I have given to you and put them in prompts.md and each time I make changes save the textual prompts there to keep track.

- Can you now add code to scraper to see the progress bar and how many websites still to go and calculate the execution time. Continue scrapig from the websites that are not scraped yet.

- I need now a script that store separate prompts to a new md file specifically for the paper related changes only in this subfolder /paper.

- Make sure this script run everytime I change chunks of text for overleaf template

- Hide in gitignore big files so I am able to import it to overleaf

- yes commit and push it

- I got all the data scraped and I want to calculate all the accesseble domains and ratio to the whole links I have in the company list. Save this info in separate markdown file.

- tokens

- Everything you mentioned for translation (do API claude call).

- my api key = <ANTHROPIC_API_KEY redacted — set via the ANTHROPIC_API_KEY env var> and use claude haiku for translation and claude opus for reasoning for innovative or not

- Now choose haiku 4.5 for everything because it is cheaper

- ping me when the batches are done and how many left still

- can you pause and show what you have done so far

- Count how my symbols are in every text file for the analysis

- Save this info to .md file  and please make a separate collection with already scraped English only websites and calculate how many of those. If you see Finnish scraped text just discard it.

- Now I need in english_only corpus perform basic text cleaning and do this sort of table Table 4 " Summary statistics for text length. Variable Definition Mean Std Min Max Char_clean Number of characters after text cleaning 3757 5275 5 123 299 Words_clean Number of words after text cleaning 460 634 1 13 946 Tokens Numeric representations of characters 915 1252 1 26 696" but with my numbers counted from my corpus.

- Publish this to github without scrape data (ignore it).
