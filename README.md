# MirrorWalk

                     "The reflection holds the memory..."

    A tool for gazing into the digital mirror and finding the words lost in the glass.

                         ┌───────────────────────────┐
                         │      THE MIRRORWALKER     │
                         │     "Text from Image"     │
                         └───────────────────────────┘

                                     .---.
                                    /  .  \
                                   |   .   |
                                   | (   ) |
                                   |  `.'  |
                                    \  .  /
                                     `---'
                                       |
                                .------|------.
                               /       |       \
                              /        |        \
                             |         |         |
                             |         |         |
                              \        |        /
                               \       |       /
                                '------|------'
                                       |
                                       |
    
    
                      ...an observer in the gallery of pixels,    
    
                             [ M I R R O R W A L K ]
                            "Your Memories, Indexed."

## Author

*   **Name:** Clive Akporube
*   **GitHub:** [whoami-pantheon](https://github.com/whoami-pantheon)
*   **LinkedIn:** [Clive Kaiser](https://linkedin.com/in/clive-kaiser)

## A Glimpse into the Glass

This tool is **MirrorWalk**, a powerful offline OCR engine and search tool. It looks at images, reads them, remembers them, and helps you find the words you've forgotten.
I built this tool because I have a habit of taking screenshots of things I find useful until my screenshot library numbers in the thousands and I end up not being able to find specific things from months ago. With this, all I need to remember is a snippet of text the screenshot contains, and I'll be able to trace the specific image.



## The Art of Reflection

MirrorWalk is a sophisticated instrument for a singular purpose: to build a searchable, offline database of text from your image collections.

*   **Cross-Platform:** Designed to work on Windows, macOS, and Linux.
*   **Parallel Processing:** Built with `concurrent.futures`, it can process images in parallel, using multiple CPU cores to rapidly build its index.
*   **Recursive Descent:** It can scan a single folder or recursively dive into a directory structure, leaving no image unread.
*   **Fuzzy & Exact Search:** Find what you're looking for with precision. Use exact matching for known phrases or fuzzy search to find partial or misspelled terms.
*   **Instant Access:** Directly open any image file from the search results, allowing you to immediately view the source.
*   **Offline & Private:** Your images and their text never leave your machine. Everything is processed and stored locally.
*   **Persistent Memory:** MirrorWalk remembers which images it has seen. Re-running it on the same folder will only process new additions, saving you time.

## The Ritual of Preparation

To begin your journey with MirrorWalk, one must first prepare the environment.

1.  **Install the OCR Engine:**
    *   **Windows:** Download and install the Tesseract engine from [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki).
    *   **Mac:** Run `brew install tesseract` in your terminal.
    *   **Linux:** Run `sudo apt install tesseract-ocr` in your terminal.

2.  **Create the Virtual Space:**
    ```bash
    python3 -m venv venv
    ```

3.  **Activate the Environment:**
    ```bash
    source venv/bin/activate
    ```

4.  **Install the Required Libraries:**
    ```bash
    pip install -r requirements.txt
    ```

## Unleashing the Walker

To command the MirrorWalk, use the interactive menu.

**Start the program:**

```bash
python3 mirrorwalk.py
```

From there, you will be presented with a menu:

1.  **Process/Re-index Folder:** Point MirrorWalk at a folder of images. It will ask if you want to scan recursively (including subfolders). The tool will then process all images, extract the text, and save it to `results.json` and `results.csv`.
2.  **Search and Open:** Search the indexed text. You can choose between an "Exact Only" search or a "Fuzzy + Exact" search. Results are displayed with context, and you can open any image directly from the search results.
3.  **Exit:** Close the program.

## The Spoils of the Walk

The extracted text and file information are stored in two files:

*   `results.json`: A detailed JSON file containing the filename, full path, extracted text, and processing time for each image.
*   `results.csv`: A simple CSV file with the filename, path, and extracted text for easy viewing in a spreadsheet.

When you perform a search, you also have the option to export the results to a `search_results.txt` file.

## A Word of Caution

MirrorWalk is a powerful tool for indexing and searching your personal data. It is designed for convenience and privacy. The creator is not responsible for the content of the images you index. Please use this tool responsibly.

## License

This project is licensed under the Apache 2.0 License.

## Acknowledgments

*   To the developers of Tesseract OCR for their incredible open-source engine.
*   To the open-source community for the libraries that make this tool possible.
*   And to you, the user, for giving your images a voice.

---
*The reflection is vast, but the words are now within reach. Happy searching.*
---  
