package com.example.menu3;

import android.os.Bundle;
import android.view.MenuItem;
import android.widget.Button;
import android.widget.PopupMenu;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        Button btn = findViewById(R.id.btn);

        btn.setOnClickListener(v -> {
            PopupMenu popup = new PopupMenu(MainActivity.this, v);
            popup.getMenuInflater().inflate(R.menu.popup, popup.getMenu());

            popup.setOnMenuItemClickListener(new PopupMenu.OnMenuItemClickListener() {
                @Override
                public boolean onMenuItemClick(MenuItem item) {
                    int id = item.getItemId();

                    if (id == R.id.java) {
                        Toast.makeText(MainActivity.this, "Java Selected", Toast.LENGTH_SHORT).show();
                        return true;
                    } else if (id == R.id.kotlin) {
                        Toast.makeText(MainActivity.this, "Kotlin Selected", Toast.LENGTH_SHORT).show();
                        return true;
                    } else if (id == R.id.html) {
                        Toast.makeText(MainActivity.this, "HTML Selected", Toast.LENGTH_SHORT).show();
                        return true;
                    }
                    return false;
                }
            });

            popup.show();
        });
    }
}