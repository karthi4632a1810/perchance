let cart = [];

function updateCartUI() {
    const itemsContainer = document.getElementById('cart-items');
    const totalElement = document.getElementById('cart-total');
    const countElement = document.getElementById('cart-count');

    if (!itemsContainer || !totalElement || !countElement) return;

    itemsContainer.innerHTML = '';
    let total = 0;

    cart.forEach((item, index) => {
        total += item.price;
        const itemEl = document.createElement('div');
        itemEl.className = 'flex gap-4 items-center';
        itemEl.innerHTML = `
            <img src="${item.img}" class="w-20 h-20 object-cover">
            <div class="flex-grow">
                <h4 class="text-sm serif">${item.name}</h4>
                <p class="text-xs text-gray-500">$${item.price}</p>
            </div>
            <button onclick="removeFromCart(${index})" class="text-xs uppercase tracking-widest">Remove</button>
        `;
        itemsContainer.appendChild(itemEl);
    });

    totalElement.innerText = `$${total.toLocaleString()}`;
    countElement.innerText = cart.length;
}

function toggleCart() {
    const drawer = document.getElementById('cart-drawer');
    const content = document.getElementById('cart-content');
    
    if (drawer.classList.contains('opacity-0')) {
        drawer.classList.remove('opacity-0');
        drawer.classList.add('opacity-100');
        content.classList.remove('translate-x-full');
    } else {
        drawer.classList.add('opacity-0');
        drawer.classList.remove('opacity-100');
        content.classList.add('translate-x-full');
    }
}

function addToCart(name, price, img) {
    cart.push({ name, price, img });
    updateCartUI();
    toggleCart();
}

function removeFromCart(index) {
    cart.splice(index, 1);
    updateCartUI();
}

// Handle "Quick Add" buttons on shop.html
document.addEventListener('DOMContentLoaded', () => {
    const quickAddButtons = document.querySelectorAll('.quick-add-btn');
    quickAddButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const name = btn.dataset.name;
            const price = parseFloat(btn.dataset.price);
            const img = btn.dataset.img;
            addToCart(name, price, img);
        });
    });
});

// Handle "Add to Bag" on product.html
const addBtn = document.getElementById('add-to-cart');
if (addBtn) {
    addBtn.addEventListener('click', () => {
        addToCart("The Heritage Chronograph", 4200, "https://images.unsplash.com/photo-1523174346687-a4e347380511?auto=format&fit=crop&q=80&w=1000");
    });
}
