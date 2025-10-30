// Main JavaScript functionality
document.addEventListener('DOMContentLoaded', function() {
    initializeSearch();
    initializeModals();
    initializeFileActions();
    initializeDragAndDrop();
});

function initializeSearch() {
    const searchInput = document.getElementById('searchInput');
    const searchResults = document.getElementById('searchResults');
    
    if (searchInput) {
        let searchTimeout;
        
        searchInput.addEventListener('input', function() {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                const query = this.value.trim();
                if (query.length >= 2) {
                    performSearch(query);
                } else {
                    hideSearchResults();
                }
            }, 300);
        });
        
        searchInput.addEventListener('focus', function() {
            const query = this.value.trim();
            if (query.length >= 2) {
                performSearch(query);
            }
        });
        
        // Hide results when clicking outside
        document.addEventListener('click', function(e) {
            if (!searchResults.contains(e.target) && e.target !== searchInput) {
                hideSearchResults();
            }
        });
    }
}

async function performSearch(query) {
    try {
        const response = await fetch(`/search/?q=${encodeURIComponent(query)}`);
        const data = await response.json();
        displaySearchResults(data.results);
    } catch (error) {
        console.error('Search error:', error);
    }
}

function displaySearchResults(results) {
    const searchResults = document.getElementById('searchResults');
    if (!searchResults) return;
    
    if (results.length === 0) {
        searchResults.innerHTML = '<div class="search-result-item">No files found</div>';
    } else {
        searchResults.innerHTML = results.map(result => `
            <div class="search-result-item" onclick="navigateToFile('${result.path}')">
                <div class="search-result-icon">${result.icon}</div>
                <div class="search-result-info">
                    <div class="search-result-name">${result.name}</div>
                    <div class="search-result-path">${result.folder}</div>
                    <div class="search-result-size">${result.formatted_size}</div>
                </div>
            </div>
        `).join('');
    }
    
    searchResults.style.display = 'block';
}

function hideSearchResults() {
    const searchResults = document.getElementById('searchResults');
    if (searchResults) {
        searchResults.style.display = 'none';
    }
}

function navigateToFile(filePath) {
    const folderPath = filePath.substring(0, filePath.lastIndexOf('/'));
    window.location.href = `/browser/${folderPath}/`;
}

function initializeModals() {
    // Create folder modal
    const createFolderBtn = document.getElementById('createFolderBtn');
    const createFolderModal = document.getElementById('createFolderModal');
    const closeModal = document.querySelector('.close-modal');
    
    if (createFolderBtn && createFolderModal) {
        createFolderBtn.addEventListener('click', () => {
            createFolderModal.style.display = 'block';
        });
        
        closeModal.addEventListener('click', () => {
            createFolderModal.style.display = 'none';
        });
        
        window.addEventListener('click', (e) => {
            if (e.target === createFolderModal) {
                createFolderModal.style.display = 'none';
            }
        });
    }
}

function initializeFileActions() {
    // File action handlers
    document.addEventListener('click', function(e) {
        if (e.target.classList.contains('download-btn')) {
            const filePath = e.target.dataset.filepath;
            downloadFile(filePath);
        } else if (e.target.classList.contains('delete-btn')) {
            const filePath = e.target.dataset.filepath;
            const fileName = e.target.dataset.filename;
            deleteFile(filePath, fileName);
        } else if (e.target.classList.contains('preview-btn')) {
            const filePath = e.target.dataset.filepath;
            previewFile(filePath);
        }
    });
}

async function createFolder() {
    const folderName = document.getElementById('folderName').value.trim();
    const folderPath = document.getElementById('currentFolderPath').value;
    
    if (!folderName) {
        alert('Please enter a folder name');
        return;
    }
    
    try {
        const response = await fetch('/create-folder/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCSRFToken()
            },
            body: JSON.stringify({
                folder_path: folderPath,
                folder_name: folderName
            })
        });
        
        const result = await response.json();
        
        if (result.success) {
            location.reload();
        } else {
            alert('Error creating folder: ' + result.error);
        }
    } catch (error) {
        alert('Error creating folder: ' + error.message);
    }
}

async function deleteFile(filePath, fileName) {
    if (!confirm(`Are you sure you want to delete "${fileName}"?`)) {
        return;
    }
    
    try {
        const response = await fetch('/delete/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCSRFToken()
            },
            body: JSON.stringify({
                path: filePath
            })
        });
        
        const result = await response.json();
        
        if (result.success) {
            location.reload();
        } else {
            alert('Error deleting file: ' + result.error);
        }
    } catch (error) {
        alert('Error deleting file: ' + error.message);
    }
}

function downloadFile(filePath) {
    window.open(`/download/${filePath}`, '_blank');
}

async function previewFile(filePath) {
    try {
        const response = await fetch(`/preview/${filePath}`);
        const result = await response.json();
        
        if (result.file) {
            // For now, show basic file info. Extend this for actual file previews
            alert(`File: ${result.file.name}\nSize: ${result.file.formatted_size}\nType: ${result.file.extension}`);
        } else {
            alert('Error previewing file: ' + result.error);
        }
    } catch (error) {
        alert('Error previewing file: ' + error.message);
    }
}

function initializeDragAndDrop() {
    // Additional drag and drop functionality can be added here
}

function getCSRFToken() {
    return document.querySelector('[name=csrfmiddlewaretoken]').value;
}

// Utility functions
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function formatTimestamp(timestamp) {
    return new Date(timestamp * 1000).toLocaleString();
}