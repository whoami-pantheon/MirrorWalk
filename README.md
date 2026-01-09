# MirrorWalk

                     "The reflection holds the memory..."

              >_ Engineering Systemic Memory & Data Hermeticism|

                         ┌───────────────────────────┐
                         │         MIRRORWALK        │
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
                           "Your Artifacts, Indexed."

## Author

*   **Name:** Clive Akporube
*   **GitHub:** [whoami-pantheon](https://github.com/whoami-pantheon)
*   **LinkedIn:** [Clive Kaiser](https://linkedin.com/in/clive-kaiser)

## Solving Information Entropy (A Glimpse into the Glass)

Organizational memory is often trapped in unindexed visual artifacts; architectural diagrams, legacy logs, and critical technical screenshots. As these libraries scale, they succumb to "Information Entropy", becoming unsearchable noise. 

MirrorWalk is a sophisticated instrument designed for a singular purpose: to build a high-fidelity, offline database of systemic memory. It transforms static visual data into a searchable semantic index, ensuring that operational knowledge is retrievable without the security risks of external telemetry.



## The Art of Intent

MirrorWalk operates on the principle of Data Hermeticism. In an era of pervasive cloud-leakage, this tool ensures that sensitive internal data remains entirely local.

*   **Cross-Platform:** Designed to work on Windows, macOS, and Linux.
*   **Parallel Processing:** Built with `concurrent.futures`, it can process images in parallel, using multiple CPU cores to rapidly build its index.
*   **Recursive Descent:** It can scan a single folder or recursively dive into a directory structure, leaving no image unread.
*   **Data Sovereignty:** Native air-gapped compatibility. Zero external telemetry; all processing and state persistence `(JSON/CSV)` occur within the local perimeter.
*   **Persistent Memory:** Implements delta-scanning to identify new artifacts since the last ritual, optimizing computational overhead. Re-running it on the same folder will only process new additions, saving you time.
*   **Fuzzy & Exact Search:** Find what you're looking for with precision. Use exact matching for known phrases or fuzzy search to find partial or misspelled terms.
*   **Instant Access:** Directly open any image file from the search results, allowing you to immediately view the source.


## The Ritual of Preparation (Systemic Integration)

To integrate MirrorWalk into your environment, the local OCR engine must be initialized.

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

    For Windows:
    ```bash
    venv\Scripts\activate
    ```
    

5.  **Install the Required Libraries:**
    ```bash
    pip install -r requirements.txt
    ```

## Unleashing the Walker

MirrorWalk provides a high-fidelity interface for data retrieval.\
To command the MirrorWalk, use the interactive menu.

**Start the program:**

```bash
python3 mirrorwalk.py
```

From there, you will be presented with a menu:

1.  **Process/Re-index Folder:** Point the Walker at a specific directory. It will execute a recursive scan, extracting text and hardening it into `results.json` and `results.csv`.
2.  **Search and Open:** Search the indexed text. `"Exact Only"` search and `"Fuzzy + Exact"` search bridge the gap between fragmented human memory and technical sources. Results are displayed with context, allowing for the immediate opening of source files.
3.  **Exit:** Close the program.

## The Spoils of the Walk (Data Persistence)

The extracted text and file information are stored in two files:

*   `results.json`: Structured technical telemetry including filename, full path, extracted text, and processing duration.
*   `results.csv`: A flattened view for rapid spreadsheet analysis and audit trails.

When you perform a search, you also have the option to export the results to a `search_results.txt` file.

## A Word of Caution (The Sovereignty Mandate)

MirrorWalk is built for Privacy Sovereignty. It eliminates the privacy-utility trade-off. However, the architect is not responsible for the nature of the data you choose to index. Use this instrument to strengthen operational resilience, not to compromise it.

## License

This project is licensed under the Apache 2.0 License.

## Attribution & Foundational Dependencies

* **Tesseract OCR**: MirrorWalk utilizes the Tesseract engine as its foundational neural layer for high-fidelity character recognition.
* **The Digital Commons**: This instrument is built upon the robust architecture of the open-source community, leveraging specialized libraries for parallel execution and fuzzy logic.
* **Operational Context**: Designed for those who recognize that unindexed data is a liability, and seek to transform it into a sovereign asset.

---
*The reflection is vast, but the words are now within reach.*
---  
